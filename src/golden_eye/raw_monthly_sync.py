from __future__ import annotations

import csv
import hashlib
import json
import re
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable

from psycopg2.extras import execute_values

from .config import Settings
from .db import connection, ident, qname
from .transport_sync import _mssql_connection, _split_table, _sql_ident
from .utils import normalize_oph


_LEGACY_SYNC_COLUMNS = {
    "db45_source_key",
    "db45_source_row_hash",
    "db45_synced_at",
}


def parse_month(value: str) -> tuple[int, int, date, date]:
    text = str(value or "").strip()
    try:
        start = datetime.strptime(text, "%Y-%m").date().replace(day=1)
    except ValueError as exc:
        raise ValueError("--month harus YYYY-MM, contoh 2026-07") from exc
    if start.month == 12:
        end = date(start.year + 1, 1, 1)
    else:
        end = date(start.year, start.month + 1, 1)
    return start.year, start.month, start, end


def _canonical(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        return format(value, ".17g")
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"sha256": hashlib.sha256(bytes(value)).hexdigest()}
    return str(value)


def source_fingerprint(
    table_name: str,
    columns: Iterable[str],
    values: Iterable[Any],
) -> str:
    payload = {
        "table": str(table_name).upper(),
        "values": [
            [str(column).upper(), _canonical(value)]
            for column, value in zip(columns, values)
        ],
    }
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _resolve(
    columns: dict[str, str],
    configured: str,
    aliases: tuple[str, ...],
    *,
    required: bool,
) -> str | None:
    for candidate in (configured, *aliases):
        actual = columns.get(str(candidate or "").upper().strip())
        if actual:
            return actual
    if required:
        raise RuntimeError(
            "Kolom source DB45 tidak ditemukan. Kandidat="
            + ",".join((configured, *aliases))
        )
    return None


def _mssql_columns(cur, schema: str, table: str) -> dict[str, str]:
    cur.execute(
        """
        SELECT COLUMN_NAME
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA=? AND TABLE_NAME=?
        ORDER BY ORDINAL_POSITION
        """,
        (schema, table),
    )
    return {str(row[0]).upper(): str(row[0]) for row in cur.fetchall()}


def _mssql_primary_key(cur, schema: str, table: str) -> list[str]:
    cur.execute(
        """
        SELECT k.COLUMN_NAME
        FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS t
        JOIN INFORMATION_SCHEMA.KEY_COLUMN_USAGE k
          ON k.CONSTRAINT_CATALOG=t.CONSTRAINT_CATALOG
         AND k.CONSTRAINT_SCHEMA=t.CONSTRAINT_SCHEMA
         AND k.CONSTRAINT_NAME=t.CONSTRAINT_NAME
        WHERE t.TABLE_SCHEMA=? AND t.TABLE_NAME=?
          AND t.CONSTRAINT_TYPE='PRIMARY KEY'
        ORDER BY k.ORDINAL_POSITION
        """,
        (schema, table),
    )
    return [str(row[0]) for row in cur.fetchall()]


def _pg_columns(conn, schema: str, table: str) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT column_name, data_type, is_identity, is_generated
            FROM information_schema.columns
            WHERE table_schema=%s AND table_name=%s
            ORDER BY ordinal_position
            """,
            (schema, table),
        )
        return [
            {
                "name": str(row[0]),
                "data_type": str(row[1]),
                "identity": str(row[2]).upper() == "YES",
                "generated": str(row[3]).upper() != "NEVER",
            }
            for row in cur.fetchall()
        ]


def _build_source_filter(
    *,
    company_col: str,
    year_col: str | None,
    month_col: str | None,
    date_col: str | None,
    status_col: str | None,
    oph_col: str,
    company: str | None,
    year: int,
    month_number: int,
    start: date,
    end: date,
    statuses: tuple[str, ...],
) -> tuple[str, list[Any]]:
    predicates: list[str] = []
    params: list[Any] = []
    if company:
        predicates.append(
            f"UPPER(LTRIM(RTRIM(CONVERT(nvarchar(100), {_sql_ident(company_col)}))))=?"
        )
        params.append(company.upper())
    if year_col and month_col:
        predicates.extend(
            [
                f"TRY_CONVERT(int, {_sql_ident(year_col)})=?",
                f"TRY_CONVERT(int, {_sql_ident(month_col)})=?",
            ]
        )
        params.extend([year, month_number])
    elif date_col:
        predicates.extend(
            [
                f"TRY_CONVERT(date, {_sql_ident(date_col)})>=?",
                f"TRY_CONVERT(date, {_sql_ident(date_col)})<?",
            ]
        )
        params.extend([start, end])
    else:
        raise RuntimeError(
            "DB45 tidak mempunyai PERIOD_YEAR+PERIOD_MONTH ataupun PERIOD_DATE"
        )
    if status_col and statuses:
        placeholders = ",".join("?" for _ in statuses)
        predicates.append(
            f"UPPER(LTRIM(RTRIM(CONVERT(nvarchar(100), {_sql_ident(status_col)})))) "
            f"IN ({placeholders})"
        )
        params.extend(statuses)
    predicates.append(f"NULLIF(LTRIM(RTRIM(CONVERT(nvarchar(255), {_sql_ident(oph_col)}))), '') IS NOT NULL")
    return " AND ".join(predicates), params


def _pg_month_filter(
    target_columns: dict[str, str],
    company: str | None,
    year: int,
    month_number: int,
    start: date,
    end: date,
    statuses: tuple[str, ...],
) -> tuple[str, list[Any], str]:
    company_col = _resolve(
        target_columns, "COMPANY_CODE", ("COMPANY", "BUKRS"), required=True
    )
    year_col = _resolve(
        target_columns, "PERIOD_YEAR", ("YEAR", "TAHUN"), required=False
    )
    month_col = _resolve(
        target_columns, "PERIOD_MONTH", ("MONTH", "BULAN"), required=False
    )
    date_col = _resolve(
        target_columns, "PERIOD_DATE", ("REPORT_DATE", "TANGGAL"), required=False
    )
    status_col = _resolve(
        target_columns, "ACCOUNT_STATUS", ("STATUS",), required=False
    )
    oph_col = _resolve(
        target_columns, "HARVEST_TICKET", ("OPH", "TIKET_PANEN", "NO_BCC"), required=True
    )
    predicates: list[str] = []
    params: list[Any] = []
    if company:
        predicates.append(f"upper(trim({ident(company_col)}::text))=%s")
        params.append(company.upper())
    if year_col and month_col:
        predicates.extend(
            [
                f"nullif(trim({ident(year_col)}::text), '')::numeric::integer=%s",
                f"nullif(trim({ident(month_col)}::text), '')::numeric::integer=%s",
            ]
        )
        params.extend([year, month_number])
    elif date_col:
        predicates.extend(
            [f"{ident(date_col)}::date >= %s", f"{ident(date_col)}::date < %s"]
        )
        params.extend([start, end])
    else:
        raise RuntimeError("RAW PostgreSQL tidak mempunyai kolom periode")
    if status_col and statuses:
        predicates.append(f"upper(trim({ident(status_col)}::text)) = ANY(%s)")
        params.append(list(statuses))
    return " AND ".join(predicates), params, oph_col


def _choose_lookup_spec(
    settings: Settings,
    target_info: list[dict[str, Any]],
    pairs: list[tuple[str, str]],
    key_columns: list[str],
    *,
    source_oph_column: str,
    source_year_column: str | None,
    source_company_column: str,
) -> dict[str, Any]:
    """Choose an indexed point lookup; never silently fall back to a heap scan."""
    target_for_source = {source.upper(): target for source, target in pairs}
    info_for_target = {item["name"].upper(): item for item in target_info}
    target_key_columns = [
        target_for_source[value.upper()]
        for value in key_columns
        if value.upper() in target_for_source
    ]
    target_oph = target_for_source.get(source_oph_column.upper())
    target_year = (
        target_for_source.get(source_year_column.upper())
        if source_year_column
        else None
    )
    target_company = target_for_source.get(source_company_column.upper())
    table_sql = qname(settings.raw_business_schema, settings.raw_business_table)
    with connection(settings) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT indexname, indexdef
            FROM pg_indexes
            WHERE schemaname=%s AND tablename=%s
            ORDER BY indexname
            """,
            (settings.raw_business_schema, settings.raw_business_table),
        )
        indexes = [(str(row[0]), str(row[1])) for row in cur.fetchall()]

    if len(target_key_columns) == 1:
        key_target = target_key_columns[0]
        key_pattern = re.compile(
            rf"\(\s*\"?{re.escape(key_target.lower())}\"?\s*(?:,|\))"
        )
        for index_name, definition in indexes:
            normalized = definition.lower()
            if key_pattern.search(normalized):
                data_type = info_for_target[key_target.upper()]["data_type"]
                type_sql = {
                    "smallint": "int2",
                    "integer": "int4",
                    "bigint": "int8",
                    "numeric": "numeric",
                    "decimal": "numeric",
                    "uuid": "uuid",
                    "text": "text",
                    "character varying": "varchar",
                    "character": "bpchar",
                }.get(str(data_type).lower())
                if type_sql:
                    return {
                        "mode": "PRIMARY_KEY_INDEX",
                        "index_name": index_name,
                        "target_lookup_column": key_target,
                        "source_lookup_column": key_columns[0],
                        "array_type": type_sql,
                    }

    if target_oph:
        for index_name, definition in indexes:
            normalized = definition.lower()
            if "normalize_oph" in normalized and target_oph.lower() in normalized:
                return {
                    "mode": "YEAR_OPH_INDEX",
                    "index_name": index_name,
                    "target_lookup_column": target_oph,
                    "source_lookup_column": source_oph_column,
                    "target_year_column": target_year,
                    "target_year_data_type": (
                        info_for_target[target_year.upper()]["data_type"]
                        if target_year
                        else None
                    ),
                    "target_company_column": target_company,
                }

    year_sql = ident(target_year) if target_year else "period_year"
    oph_sql = ident(target_oph) if target_oph else "harvest_ticket"
    raise RuntimeError(
        "RAW_LOOKUP_INDEX_MISSING: sync cepat menolak full scan. Buat index ini "
        "satu kali dari DBeaver, tunggu selesai, lalu rerun:\n"
        f"CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_raw_sync_year_oph "
        f"ON {table_sql} ({year_sql}, processing.normalize_oph({oph_sql}::text));"
    )


def _load_existing_batch_keys(
    settings: Settings,
    source_table_name: str,
    pairs: list[tuple[str, str]],
    key_columns: list[str],
    lookup_spec: dict[str, Any],
    lookup_values: list[Any],
    *,
    year: int,
    company: str | None,
) -> tuple[dict[str, str], int, int]:
    """Fetch existing RAW rows through an already available narrow index."""
    if not lookup_values:
        return {}, 0, 0
    table_sql = qname(settings.raw_business_schema, settings.raw_business_table)
    pair_source = [source for source, _target in pairs]
    pair_target = [target for _source, target in pairs]
    target_for_source = {source.upper(): target for source, target in pairs}
    target_key_columns = [
        target_for_source[value.upper()]
        for value in key_columns
        if value.upper() in target_for_source
    ]
    key_indexes = [pair_target.index(value) for value in target_key_columns]
    params: list[Any] = []
    if lookup_spec["mode"] == "PRIMARY_KEY_INDEX":
        predicate = (
            f"{ident(lookup_spec['target_lookup_column'])} "
            f"= ANY(%s::{lookup_spec['array_type']}[])"
        )
        params.append(list(dict.fromkeys(lookup_values)))
    else:
        predicates = [
            f"processing.normalize_oph("
            f"{ident(lookup_spec['target_lookup_column'])}::text) = ANY(%s)"
        ]
        params.append(list(dict.fromkeys(str(value) for value in lookup_values if value)))
        target_year = lookup_spec.get("target_year_column")
        if target_year:
            numeric_types = {
                "smallint", "integer", "bigint", "numeric", "decimal",
                "real", "double precision",
            }
            if str(lookup_spec.get("target_year_data_type") or "").lower() in numeric_types:
                predicates.append(f"{ident(target_year)}=%s")
                params.append(year)
            else:
                predicates.append(f"trim({ident(target_year)}::text)=%s")
                params.append(str(year))
        target_company = lookup_spec.get("target_company_column")
        if target_company and company:
            predicates.append(f"upper(trim({ident(target_company)}::text))=%s")
            params.append(company.upper())
        predicate = " AND ".join(predicates)

    existing: dict[str, str] = {}
    duplicate_keys = 0
    total = 0
    with connection(settings) as conn, conn.cursor() as cur:
        cur.execute(
            f"SELECT {', '.join(ident(value) for value in pair_target)} "
            f"FROM {table_sql} WHERE {predicate}",
            params,
        )
        for row in cur.fetchall():
            total += 1
            row_hash = source_fingerprint(source_table_name, pair_source, row)
            if key_indexes:
                source_key = source_fingerprint(
                    source_table_name + ":KEY",
                    key_columns,
                    [row[index] for index in key_indexes],
                )
            else:
                source_key = row_hash
            if source_key in existing:
                duplicate_keys += 1
            existing[source_key] = row_hash
    return existing, total, duplicate_keys


def _write_report(path: str | Path | None, metrics: dict[str, Any]) -> str:
    report = Path(path or "raw_monthly_sync_report.csv").resolve()
    report.parent.mkdir(parents=True, exist_ok=True)
    with report.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow(["metric", "value"])
        for key, value in metrics.items():
            writer.writerow(
                [key, json.dumps(value, ensure_ascii=False, default=str) if isinstance(value, (dict, list, tuple)) else value]
            )
    return str(report)


def sync_raw_production_month(
    settings: Settings,
    source_group: str,
    month_value: str,
    *,
    batch_size: int = 1000,
    apply: bool = False,
    report_path: str | Path | None = None,
) -> dict[str, Any]:
    source_group = str(source_group or "").upper().strip()
    if source_group not in {"PWP", "DIN", "DAN", "DWT", "ALL"}:
        raise ValueError("--source-group harus PWP, DIN, DAN, DWT, atau ALL")
    if not 100 <= int(batch_size) <= 10000:
        raise ValueError("--batch-size sync-raw-month harus 100..10000")
    year, month_number, start, end = parse_month(month_value)
    month_text = start.strftime("%Y-%m")
    company = None if source_group == "ALL" else settings.source_company(source_group)
    status_values = (
        tuple(settings.account_status_values or ())
        if source_group == "ALL"
        else settings.account_status_values_for(source_group)
    )
    schema, table = _split_table(settings.mssql_raw_table)
    source_table_sql = f"{_sql_ident(schema)}.{_sql_ident(table)}"

    print("=" * 78)
    print(
        f"DB45 -> RAW MONTHLY SYNC: {source_group} | month={month_text} | apply={apply}"
    )
    print("=" * 78)
    print(
        "FAST MODE: tidak menjalankan COUNT DISTINCT/full scan RAW sebelum load.",
        flush=True,
    )

    mssql = _mssql_connection(settings)
    try:
        cur = mssql.cursor()
        source_columns = _mssql_columns(cur, schema, table)
        company_col = _resolve(
            source_columns,
            settings.mssql_raw_company_column,
            ("COMPANY", "BUKRS"),
            required=True,
        )
        year_col = _resolve(
            source_columns,
            settings.mssql_raw_year_column,
            ("YEAR", "TAHUN"),
            required=False,
        )
        month_col = _resolve(
            source_columns,
            settings.mssql_raw_month_column,
            ("MONTH", "BULAN"),
            required=False,
        )
        date_col = _resolve(
            source_columns,
            settings.mssql_raw_date_column,
            ("REPORT_DATE", "TANGGAL", "DATE"),
            required=not bool(year_col and month_col),
        )
        status_col = _resolve(
            source_columns,
            settings.mssql_raw_status_column,
            ("STATUS",),
            required=False,
        )
        oph_col = _resolve(
            source_columns,
            settings.mssql_raw_oph_column,
            ("OPH", "TIKET_PANEN", "NO_BCC"),
            required=True,
        )
        source_where, source_params = _build_source_filter(
            company_col=company_col,
            year_col=year_col,
            month_col=month_col,
            date_col=date_col,
            status_col=status_col,
            oph_col=oph_col,
            company=company,
            year=year,
            month_number=month_number,
            start=start,
            end=end,
            statuses=status_values,
        )
        cur.execute(
            f"SELECT COUNT_BIG(*), COUNT(DISTINCT CONVERT(nvarchar(255), {_sql_ident(oph_col)})) "
            f"FROM {source_table_sql} WHERE {source_where}",
            source_params,
        )
        count_row = cur.fetchone()
        source_count = int(count_row[0] or 0)
        source_oph = int(count_row[1] or 0)

        with connection(settings) as pg_conn:
            target_info = _pg_columns(
                pg_conn, settings.raw_business_schema, settings.raw_business_table
            )
        writable_target = {
            item["name"].upper(): item["name"]
            for item in target_info
            if not item["identity"]
            and not item["generated"]
            and item["name"] not in _LEGACY_SYNC_COLUMNS
        }
        pairs = [
            (source_columns[upper], target)
            for upper, target in writable_target.items()
            if upper in source_columns
        ]
        if not pairs:
            raise RuntimeError("Tidak ada irisan kolom antara DB45 dan RAW PostgreSQL")

        configured_keys = [
            source_columns[value]
            for value in (settings.mssql_raw_key_columns or ())
            if value in source_columns
        ]
        missing_configured = [
            value
            for value in (settings.mssql_raw_key_columns or ())
            if value not in source_columns
        ]
        if missing_configured:
            raise RuntimeError(
                "MSSQL_RAW_KEY_COLUMNS tidak ditemukan: "
                + ", ".join(missing_configured)
            )
        pk_columns = configured_keys or _mssql_primary_key(cur, schema, table)
        mapped_source_columns = {source.upper() for source, _target in pairs}
        key_is_mapped = bool(pk_columns) and all(
            value.upper() in mapped_source_columns for value in pk_columns
        )
        effective_key_columns = pk_columns if key_is_mapped else []
        if configured_keys and key_is_mapped:
            key_mode = "CONFIGURED_KEY"
        elif pk_columns and key_is_mapped:
            key_mode = "MSSQL_PRIMARY_KEY"
        elif pk_columns:
            key_mode = "FULL_ROW_FINGERPRINT_UNMAPPED_PRIMARY_KEY"
        else:
            key_mode = "FULL_ROW_FINGERPRINT"
        lookup_spec = _choose_lookup_spec(
            settings,
            target_info,
            pairs,
            effective_key_columns,
            source_oph_column=oph_col,
            source_year_column=year_col,
            source_company_column=company_col,
        )
        select_columns = [source for source, _target in pairs]
        for key_col in pk_columns:
            if key_col not in select_columns:
                select_columns.append(key_col)
        metrics: dict[str, Any] = {
            "source_group": source_group,
            "company_code": company or "ALL",
            "month": month_text,
            "source": f"{settings.mssql_server}/{settings.mssql_database}/{schema}.{table}",
            "target": f"{settings.raw_business_schema}.{settings.raw_business_table}",
            "source_rows": source_count,
            "source_distinct_oph": source_oph,
            "raw_precount": "SKIPPED_FAST_MODE",
            "mapped_columns": len(pairs),
            "key_mode": key_mode,
            "lookup_mode": lookup_spec["mode"],
            "lookup_index": lookup_spec["index_name"],
            "source_primary_key_columns": pk_columns,
            "dedup_key_columns": effective_key_columns,
            "existing_month_rows_scanned": 0,
            "existing_month_keys": 0,
            "existing_duplicate_keys": 0,
            "existing_different_hash": 0,
            "inserted": 0,
            "updated": 0,
            "skipped": 0,
            "source_duplicate_keys": 0,
            "fetched": 0,
            "applied": bool(apply),
        }
        print(
            f"DB45 rows={source_count:,} distinct_oph={source_oph:,}; "
            f"columns={len(pairs)}; key_mode={key_mode}; "
            f"lookup={lookup_spec['mode']}:{lookup_spec['index_name']}; "
            f"account_status_scope={list(status_values) if status_values else 'ALL'}",
            flush=True,
        )
        if not apply:
            metrics["report"] = _write_report(report_path, metrics)
            print("DRY-RUN saja; RAW belum diubah. Tambahkan --apply setelah preflight.")
            return metrics
        if source_count == 0:
            raise RuntimeError(
                f"DB45 tidak mempunyai transaksi {source_group} {month_text} "
                + (
                    "dengan ACCOUNT_STATUS yang dikonfigurasi"
                    if status_values
                    else "dengan HARVEST_TICKET yang tidak kosong"
                )
            )

        print(
            "[RAW SYNC] mulai stream DB45; existing RAW diperiksa per batch "
            f"melalui index {lookup_spec['index_name']} ...",
            flush=True,
        )
        select_sql = ", ".join(_sql_ident(value) for value in select_columns)
        cur.execute(
            f"SELECT {select_sql} FROM {source_table_sql} WHERE {source_where} "
            + (
                "ORDER BY " + ", ".join(_sql_ident(value) for value in pk_columns)
                if pk_columns
                else ""
            ),
            source_params,
        )
        cur.arraysize = int(batch_size)
        selected_index = {name: index for index, name in enumerate(select_columns)}
        pair_indexes = [selected_index[source] for source, _target in pairs]
        key_indexes = [selected_index[name] for name in effective_key_columns]
        lookup_source_index = selected_index[lookup_spec["source_lookup_column"]]
        target_columns = [target for _source, target in pairs]
        target_sql = qname(settings.raw_business_schema, settings.raw_business_table)
        insert_sql = (
            f"INSERT INTO {target_sql} "
            f"({', '.join(ident(value) for value in target_columns)}) VALUES %s"
        )

        batch_number = 0
        while True:
            fetched_rows = cur.fetchmany(int(batch_size))
            if not fetched_rows:
                break
            batch_number += 1
            metrics["fetched"] += len(fetched_rows)
            prepared: dict[str, tuple[list[Any], str]] = {}
            if lookup_spec["mode"] == "YEAR_OPH_INDEX":
                lookup_values = [
                    normalize_oph(row[lookup_source_index]) for row in fetched_rows
                ]
            else:
                lookup_values = [row[lookup_source_index] for row in fetched_rows]
            for source_row in fetched_rows:
                target_values = [source_row[index] for index in pair_indexes]
                row_hash = source_fingerprint(
                    f"{schema}.{table}",
                    [source for source, _target in pairs],
                    target_values,
                )
                if key_indexes:
                    key_values = [source_row[index] for index in key_indexes]
                    source_key = source_fingerprint(
                        f"{schema}.{table}:KEY", effective_key_columns, key_values
                    )
                else:
                    source_key = row_hash
                if source_key in prepared:
                    metrics["source_duplicate_keys"] += 1
                prepared[source_key] = (target_values, row_hash)

            batch_existing, existing_rows, existing_duplicate_keys = (
                _load_existing_batch_keys(
                    settings,
                    f"{schema}.{table}",
                    pairs,
                    effective_key_columns,
                    lookup_spec,
                    lookup_values,
                    year=year,
                    company=company,
                )
            )
            metrics["existing_month_rows_scanned"] += existing_rows
            metrics["existing_month_keys"] += len(batch_existing)
            metrics["existing_duplicate_keys"] += existing_duplicate_keys
            write_rows: list[tuple[Any, ...]] = []
            for key, (values, row_hash) in prepared.items():
                old_hash = batch_existing.get(key)
                if old_hash is not None:
                    metrics["skipped"] += 1
                    if old_hash != row_hash:
                        metrics["existing_different_hash"] += 1
                    continue
                write_rows.append(tuple(values))
            with connection(settings) as pg_conn, pg_conn.cursor() as pg_cur:
                if write_rows:
                    execute_values(
                        pg_cur,
                        insert_sql,
                        write_rows,
                        page_size=min(int(batch_size), 1000),
                    )
            metrics["inserted"] += len(write_rows)
            print(
                f"[RAW SYNC] batch={batch_number} fetched={metrics['fetched']:,}/"
                f"{source_count:,} inserted={metrics['inserted']:,} "
                f"updated={metrics['updated']:,} skipped={metrics['skipped']:,}",
                flush=True,
            )
    finally:
        mssql.close()

    metrics["covered_source_rows"] = (
        int(metrics["inserted"])
        + int(metrics["skipped"])
        + int(metrics["source_duplicate_keys"])
    )
    metrics["report"] = _write_report(report_path, metrics)
    if int(metrics["fetched"]) != int(source_count):
        raise RuntimeError(
            f"RAW sync tidak lengkap: fetched={metrics['fetched']:,} source={source_count:,}"
        )
    if int(metrics["covered_source_rows"]) != int(source_count):
        raise RuntimeError(
            "RAW sync gagal audit coverage: "
            f"covered={metrics['covered_source_rows']:,} source={source_count:,}"
        )
    print(
        f"[RAW SYNC] DONE inserted={metrics['inserted']:,} "
        f"updated={metrics['updated']:,} skipped={metrics['skipped']:,}; "
        f"covered={metrics['covered_source_rows']:,}/{source_count:,}; "
        f"report={metrics['report']}",
        flush=True,
    )
    if str(metrics["key_mode"]).startswith("FULL_ROW_FINGERPRINT"):
        print(
            "WARN primary key DB45 tidak tersedia pada kolom target RAW. Exact row "
            "tetap deduplicated, tetapi koreksi isi row lama tidak ditimpa otomatis. "
            "Pastikan kolom PK ikut tersedia pada raw.production_plantation.",
            flush=True,
        )
    return metrics
