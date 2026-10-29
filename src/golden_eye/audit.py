from __future__ import annotations

import csv
import time
from pathlib import Path
from typing import Any

import psycopg2
from psycopg2.extras import RealDictCursor, execute_values

from .config import Settings
from .context import ALIASES as BUSINESS_ALIASES
from .context import _evidence_match_sql
from .context import _find as _find_business_column
from .context import _period_date_sql
from .db import (
    connection,
    fetch_all_dict,
    ident,
    named_cursor_batches,
    qname,
    table_columns,
    table_exists,
)
from .enrichment import _raw_values


_LEGACY_UNSAFE_OPH_METHODS = (
    "MATCH_BY_RAW_ACTOR_DATE_UNIQUE_ESTATE",
    "MATCH_BY_RAW_ACTOR_DATE_UNIQUE_ESTATE_NO_OPH",
)


def _is_physical_bytea_read_error(exc: psycopg2.Error) -> bool:
    """Recognize PostgreSQL errors caused by missing/truncated relation segments."""
    sqlstate = str(getattr(exc, "pgcode", "") or "")
    message = str(exc).lower()
    if sqlstate == "XX001":
        return True
    if sqlstate != "XX000":
        return False
    markers = (
        "could not read blocks",
        "read only 0 of 8192 bytes",
        "could not open file",
        "previous segment is only",
        "invalid page in block",
        "unexpected zero page",
    )
    return any(marker in message for marker in markers)


def _probe_bytea_batch(
    conn,
    image_table: str,
    rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Read/hash BYTEA and bisect only when PostgreSQL reports corruption.

    CTID is used only inside this read-only audit so the probe does not depend
    on the source file_uid data type. Source ingestion/VACUUM FULL must not run
    concurrently because those operations can move CTIDs.
    """
    if not rows:
        return [], []

    placeholders = ", ".join(["%s::tid"] * len(rows))
    sql = f"""
        SELECT
            ctid::text AS row_ctid,
            file_uid::text AS file_uid,
            octet_length(image_bytes)::bigint AS actual_bytea_len,
            md5(image_bytes) AS bytea_md5
        FROM {image_table}
        WHERE ctid IN ({placeholders})
        ORDER BY file_uid::text
    """
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql, [row["row_ctid"] for row in rows])
            checks = {str(row["file_uid"]): dict(row) for row in cur.fetchall()}
        conn.commit()

        healthy: list[dict[str, Any]] = []
        missing: list[dict[str, Any]] = []
        for meta in rows:
            check = checks.get(str(meta["file_uid"]))
            if check is None:
                missing.append(
                    {
                        **meta,
                        "error_code": "ROW_MOVED_OR_MISSING",
                        "error_message": "CTID berubah/hilang saat audit; hentikan writer lalu ulangi audit",
                    }
                )
                continue
            healthy.append({**meta, **check})
        return healthy, missing
    except psycopg2.Error as exc:
        conn.rollback()
        if not _is_physical_bytea_read_error(exc):
            raise
        if len(rows) == 1:
            return [], [
                {
                    **rows[0],
                    "error_code": "DB_BYTEA_DATA_CORRUPTED",
                    "error_message": (
                        f"SQLSTATE={getattr(exc, 'pgcode', None)} "
                        f"{str(exc).strip()}"
                    )[:2000],
                }
            ]

        middle = len(rows) // 2
        healthy_left, failed_left = _probe_bytea_batch(conn, image_table, rows[:middle])
        healthy_right, failed_right = _probe_bytea_batch(conn, image_table, rows[middle:])
        return healthy_left + healthy_right, failed_left + failed_right


def audit_bytea_health(
    settings: Settings,
    source_group: str,
    *,
    batch_size: int = 256,
    report_path: str | None = None,
) -> dict[str, Any]:
    """Force a full BYTEA read for every source row and quarantine failures."""
    source_group = source_group.upper().strip()
    if batch_size < 1 or batch_size > 2000:
        raise ValueError("--batch-size harus 1..2000")

    image_table = qname(settings.silver_schema, settings.image_table)
    report = Path(report_path or f"bytea_audit_{source_group.lower()}.csv").resolve()
    metadata_sql = f"""
        SELECT
            ctid::text AS row_ctid,
            file_uid::text AS file_uid,
            file_name::text AS file_name,
            file_size_bytes::bigint AS expected_file_size,
            bytea_len::bigint AS stored_bytea_len
        FROM {image_table}
        WHERE upper(source_group::text)=upper(%s)
          AND image_bytes IS NOT NULL
        ORDER BY file_uid
    """

    total = readable = corrupt = size_mismatch = 0
    failed_rows: list[dict[str, Any]] = []
    report_rows: list[dict[str, Any]] = []
    started = time.time()
    print(
        f"[BYTEA AUDIT] source={source_group} batch={batch_size:,} "
        "mode=full-read-md5 (bukan sekadar IS NOT NULL)"
    )
    print("[BYTEA AUDIT] hentikan loader/VACUUM FULL selama audit karena CTID harus stabil")

    with connection(settings) as metadata_conn, connection(settings) as probe_conn:
        for batch_no, metadata_rows in enumerate(
            named_cursor_batches(
                metadata_conn,
                metadata_sql,
                (source_group,),
                batch_size,
            ),
            start=1,
        ):
            healthy, failed = _probe_bytea_batch(probe_conn, image_table, metadata_rows)
            total += len(metadata_rows)
            readable += len(healthy)
            corrupt += sum(
                row.get("error_code") == "DB_BYTEA_DATA_CORRUPTED" for row in failed
            )
            failed_rows.extend(failed)

            for row in healthy:
                expected = row.get("stored_bytea_len")
                if expected is None:
                    expected = row.get("expected_file_size")
                actual = row.get("actual_bytea_len")
                if expected is not None and actual is not None and int(expected) != int(actual):
                    size_mismatch += 1
                    report_rows.append(
                        {
                            **row,
                            "error_code": "BYTEA_SIZE_MISMATCH",
                            "error_message": f"expected={expected} actual={actual}",
                        }
                    )
            report_rows.extend(failed)

            elapsed = max(0.001, time.time() - started)
            print(
                f"[BYTEA AUDIT] batch={batch_no:,} checked={total:,} "
                f"readable={readable:,} corrupt={corrupt:,} "
                f"size_mismatch={size_mismatch:,} speed={total / elapsed:,.1f} img/s"
            )
            for row in failed:
                print(
                    f"[BYTEA AUDIT][{row['error_code']}] "
                    f"file_uid={row['file_uid']} file={row.get('file_name') or '-'}"
                )

    report.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "file_uid",
        "file_name",
        "error_code",
        "error_message",
        "expected_file_size",
        "stored_bytea_len",
        "actual_bytea_len",
        "bytea_md5",
        "row_ctid",
    ]
    with report.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(report_rows)

    row_moved_or_missing = sum(
        row.get("error_code") == "ROW_MOVED_OR_MISSING" for row in failed_rows
    )
    if row_moved_or_missing:
        raise RuntimeError(
            f"Ada {row_moved_or_missing:,} row yang berpindah/hilang selama audit. "
            "Karantina lama tidak diubah. Hentikan writer/VACUUM FULL lalu ulangi."
        )

    # Update quarantine only after the complete scan succeeds. An interrupted
    # audit therefore never removes the previous protection list.
    actual_corrupt = [
        row for row in failed_rows if row.get("error_code") == "DB_BYTEA_DATA_CORRUPTED"
    ]
    with connection(settings) as write_conn, write_conn.cursor() as cur:
        cur.execute(
            """
            UPDATE processing.source_bytea_exception
            SET is_active=false, last_checked_at=now()
            WHERE source_group=%s AND is_active=true
            """,
            (source_group,),
        )
        if actual_corrupt:
            execute_values(
                cur,
                """
                INSERT INTO processing.source_bytea_exception
                    (source_group, file_uid, file_name, error_code, error_message, is_active)
                VALUES %s
                ON CONFLICT (source_group, file_uid) DO UPDATE SET
                    file_name=excluded.file_name,
                    error_code=excluded.error_code,
                    error_message=excluded.error_message,
                    last_checked_at=now(),
                    is_active=true
                """,
                [
                    (
                        source_group,
                        row["file_uid"],
                        row.get("file_name"),
                        row["error_code"],
                        row.get("error_message"),
                        True,
                    )
                    for row in actual_corrupt
                ],
                page_size=500,
            )

    result = {
        "source_group": source_group,
        "checked": total,
        "readable": readable,
        "corrupt": corrupt,
        "row_moved_or_missing": row_moved_or_missing,
        "size_mismatch": size_mismatch,
        "report_path": str(report),
    }
    print(
        f"[BYTEA AUDIT] DONE checked={total:,} readable={readable:,} "
        f"corrupt={corrupt:,} size_mismatch={size_mismatch:,} report={report}"
    )
    return result


def audit_source(settings: Settings, source_group: str) -> dict[str, Any]:
    source_group = source_group.upper()
    image_table = qname(settings.silver_schema, settings.image_table)
    ext_regex = settings.valid_extension_regex()
    excluded_regex = settings.excluded_filename_regex()

    with connection(settings) as conn:
        source = fetch_all_dict(
            conn,
            f"""
            WITH source AS (
                SELECT
                    i.file_uid::text AS file_uid,
                    i.file_name::text AS file_name,
                    upper(trim(i.file_name::text)) AS logical_name,
                    coalesce(i.is_transport, false) AS is_transport,
                    coalesce(i.file_exists, true) AS file_exists,
                    coalesce(i.zero_byte, false) AS zero_byte,
                    (r.image_bytes IS NOT NULL OR
                     (bx.file_uid IS NULL AND i.image_bytes IS NOT NULL)) AS has_bytea,
                    left(lower(i.file_name::text), 7) = 'before_' AS is_before,
                    lower(i.file_name::text) ~ %s AS valid_extension,
                    upper(i.file_name::text) ~ %s AS excluded_token
                FROM {image_table} i
                LEFT JOIN processing.source_bytea_exception bx
                  ON bx.source_group=upper(i.source_group::text)
                 AND bx.file_uid=i.file_uid::text
                 AND bx.is_active=true
                LEFT JOIN processing.gambar_reused r
                  ON r.source_group=upper(i.source_group::text)
                 AND r.file_uid=i.file_uid::text
                 AND r.is_active=true
                WHERE upper(i.source_group::text)=upper(%s)
            )
            SELECT
                count(*)::bigint AS source_files,
                count(*) FILTER (WHERE is_transport)::bigint AS transport_files,
                count(*) FILTER (WHERE NOT is_transport)::bigint AS harvest_files,
                count(*) FILTER (WHERE NOT file_exists)::bigint AS missing_files,
                count(*) FILTER (WHERE zero_byte)::bigint AS zero_byte_files,
                count(*) FILTER (WHERE NOT has_bytea)::bigint AS without_bytea,
                count(*) FILTER (WHERE is_before)::bigint AS excluded_before,
                count(*) FILTER (WHERE excluded_token)::bigint AS excluded_ai_token,
                count(*) FILTER (WHERE NOT valid_extension)::bigint AS excluded_extension,
                count(*) FILTER (
                    WHERE NOT is_transport AND file_exists AND NOT zero_byte AND has_bytea
                      AND NOT is_before AND valid_extension AND NOT excluded_token
                )::bigint AS eligible_harvest_rows,
                count(DISTINCT logical_name) FILTER (
                    WHERE NOT is_transport AND file_exists AND NOT zero_byte AND has_bytea
                      AND NOT is_before AND valid_extension AND NOT excluded_token
                )::bigint AS eligible_harvest,
                count(*) FILTER (
                    WHERE is_transport AND file_exists AND NOT zero_byte AND has_bytea
                      AND valid_extension AND NOT excluded_token
                )::bigint AS eligible_transport
            FROM source
            """,
            (ext_regex, excluded_regex, source_group),
        )[0]

        pipeline = fetch_all_dict(
            conn,
            f"""
            SELECT
                count(DISTINCT upper(trim(i.file_name::text))) FILTER (
                    WHERE f.is_valid=true AND coalesce(i.is_transport, false)=false
                )::bigint AS embedded_harvest,
                count(DISTINCT upper(trim(i.file_name::text))) FILTER (
                    WHERE f.is_valid=true AND coalesce(i.is_transport, false)=true
                )::bigint AS embedded_transport,
                count(*) FILTER (
                    WHERE f.is_valid=false
                )::bigint AS embedding_failed
            FROM processing.image_feature f
            JOIN {image_table} i ON i.file_uid::text=f.file_uid
            LEFT JOIN processing.source_bytea_exception bx
              ON bx.source_group=upper(i.source_group::text)
             AND bx.file_uid=i.file_uid::text
             AND bx.is_active=true
            LEFT JOIN processing.gambar_reused r
              ON r.source_group=upper(i.source_group::text)
             AND r.file_uid=i.file_uid::text
             AND r.is_active=true
            WHERE upper(i.source_group::text)=upper(%s)
              AND f.model_version=%s
              AND coalesce(i.file_exists, true)=true
              AND coalesce(i.zero_byte, false)=false
              AND (r.image_bytes IS NOT NULL OR i.image_bytes IS NOT NULL)
              AND left(lower(i.file_name::text), 7) <> 'before_'
              AND lower(i.file_name::text) ~ %s
              AND upper(i.file_name::text) !~ %s
              AND (bx.file_uid IS NULL OR r.file_uid IS NOT NULL)
            """,
            (
                source_group,
                settings.model_version,
                ext_regex,
                excluded_regex,
            ),
        )[0]

        context = fetch_all_dict(
            conn,
            f"""
            WITH eligible AS (
                SELECT DISTINCT ON (upper(trim(i.file_name::text)))
                    i.file_uid::text AS file_uid,
                    c.file_uid AS context_uid,
                    c.period_date,
                    c.estate,
                    c.match_method
                FROM {image_table} i
                LEFT JOIN processing.image_context c ON c.file_uid=i.file_uid::text
                LEFT JOIN processing.source_bytea_exception bx
                  ON bx.source_group=upper(i.source_group::text)
                 AND bx.file_uid=i.file_uid::text
                 AND bx.is_active=true
                LEFT JOIN processing.gambar_reused r
                  ON r.source_group=upper(i.source_group::text)
                 AND r.file_uid=i.file_uid::text
                 AND r.is_active=true
                WHERE upper(i.source_group::text)=upper(%s)
                  AND coalesce(i.is_transport, false)=false
                  AND coalesce(i.file_exists, true)=true
                  AND coalesce(i.zero_byte, false)=false
                  AND (r.image_bytes IS NOT NULL OR i.image_bytes IS NOT NULL)
                  AND (bx.file_uid IS NULL OR r.file_uid IS NOT NULL)
                  AND left(lower(i.file_name::text), 7) <> 'before_'
                  AND lower(i.file_name::text) ~ %s
                  AND upper(i.file_name::text) !~ %s
                ORDER BY
                    upper(trim(i.file_name::text)),
                    (c.period_date IS NOT NULL) DESC,
                    i.loaded_at DESC NULLS LAST,
                    i.file_uid::text
            )
            SELECT
                count(*) FILTER (WHERE context_uid IS NOT NULL)::bigint AS context_matched,
                count(*) FILTER (
                    WHERE context_uid IS NOT NULL
                      AND coalesce(match_method, '') <> 'MATCH_DATE_FROM_FILENAME'
                )::bigint AS business_context_matched,
                count(*) FILTER (
                    WHERE match_method = 'MATCH_DATE_FROM_FILENAME'
                )::bigint AS filename_date_fallback,
                count(*) FILTER (WHERE period_date IS NOT NULL)::bigint AS context_with_month,
                count(*) FILTER (WHERE context_uid IS NULL)::bigint AS context_unmatched,
                count(*) FILTER (WHERE context_uid IS NOT NULL AND period_date IS NULL)::bigint AS context_without_month
            FROM eligible
            """,
            (source_group, ext_regex, excluded_regex),
        )[0]

        gold = fetch_all_dict(
            conn,
            """
            SELECT
                count(*)::bigint AS active_gold_groups,
                coalesce(sum(usage_count), 0)::bigint AS active_gold_usages,
                coalesce(max(usage_count), 0)::bigint AS active_gold_max_group
            FROM gold.reused_photo_group_summary
            WHERE upper(source_group)=upper(%s) AND is_active=true
            """,
            (source_group,),
        )[0]

        bytea_health = fetch_all_dict(
            conn,
            """
            SELECT
                count(*)::bigint AS bytea_corrupt_raw,
                count(*) FILTER (WHERE r.file_uid IS NOT NULL)::bigint AS bytea_recovered,
                count(*) FILTER (WHERE r.file_uid IS NULL)::bigint AS bytea_quarantined
            FROM processing.source_bytea_exception bx
            LEFT JOIN processing.gambar_reused r
              ON r.source_group=bx.source_group
             AND r.file_uid=bx.file_uid
             AND r.is_active=true
            WHERE bx.source_group=%s AND bx.is_active=true
            """,
            (source_group,),
        )[0]

    result = {**source, **pipeline, **context, **gold, **bytea_health}
    result["duplicate_logical_rows"] = max(
        0,
        int(result.get("eligible_harvest_rows") or 0)
        - int(result.get("eligible_harvest") or 0),
    )
    eligible = int(result.get("eligible_harvest") or 0)
    result["embedding_coverage_pct"] = round(
        100.0 * int(result.get("embedded_harvest") or 0) / eligible, 2
    ) if eligible else 0.0
    result["context_coverage_pct"] = round(
        100.0 * int(result.get("context_matched") or 0) / eligible, 2
    ) if eligible else 0.0
    result["business_context_coverage_pct"] = round(
        100.0 * int(result.get("business_context_matched") or 0) / eligible, 2
    ) if eligible else 0.0
    result["month_coverage_pct"] = round(
        100.0 * int(result.get("context_with_month") or 0) / eligible, 2
    ) if eligible else 0.0
    return result


def print_source_audit(source_group: str, result: dict[str, Any]) -> None:
    print("=" * 78)
    print(f"SOURCE AUDIT: {source_group.upper()}")
    print("=" * 78)
    rows = [
        ("Semua row image source", "source_files"),
        ("File panen", "harvest_files"),
        ("File transport", "transport_files"),
        ("Exclude file missing", "missing_files"),
        ("Exclude zero byte", "zero_byte_files"),
        ("Exclude tanpa BYTEA", "without_bytea"),
        ("Exclude prefix before_", "excluded_before"),
        ("Exclude token AI", "excluded_ai_token"),
        ("Exclude extension", "excluded_extension"),
        ("Eligible panen sebelum dedup", "eligible_harvest_rows"),
        ("Duplicate logical filename", "duplicate_logical_rows"),
        ("Eligible panen untuk similarity", "eligible_harvest"),
        ("Eligible transport", "eligible_transport"),
        ("Embedding panen valid", "embedded_harvest"),
        ("Embedding transport valid", "embedded_transport"),
        ("Embedding gagal", "embedding_failed"),
        ("BYTEA korup pada source asli", "bytea_corrupt_raw"),
        ("BYTEA sudah recovery overlay", "bytea_recovered"),
        ("BYTEA belum dapat dipulihkan", "bytea_quarantined"),
        ("Context matched", "context_matched"),
        ("Business context matched", "business_context_matched"),
        ("Tanggal fallback filename", "filename_date_fallback"),
        ("Context punya bulan", "context_with_month"),
        ("Context tidak match", "context_unmatched"),
        ("Context match tanpa bulan", "context_without_month"),
        ("Gold active groups", "active_gold_groups"),
        ("Gold active usages", "active_gold_usages"),
        ("Gold max group", "active_gold_max_group"),
    ]
    for label, key in rows:
        print(f"{label:<35}: {int(result.get(key) or 0):>12,}")
    print(f"{'Coverage embedding':<35}: {result['embedding_coverage_pct']:>11.2f}%")
    print(f"{'Coverage context':<35}: {result['context_coverage_pct']:>11.2f}%")
    print(f"{'Coverage business context':<35}: {result['business_context_coverage_pct']:>11.2f}%")
    print(f"{'Coverage bulan':<35}: {result['month_coverage_pct']:>11.2f}%")


def _month_audit_aliases(source_group: str) -> tuple[str, list[str]]:
    source = source_group.upper().strip()
    if source in {"DAN", "ELHP_DAN"}:
        return "DAN", ["DAN", "ELHP_DAN"]
    if source in {"DWT", "ELHP_DWT"}:
        return "DWT", ["DWT", "ELHP_DWT"]
    return source, [source]


def audit_elhp_estate_scope(
    settings: Settings,
    source_group: str,
) -> dict[str, Any]:
    """Prove that ELHP dashboard/report rows obey the company estate owner.

    This audit deliberately reports both layers: the immutable active Gold
    history (where old mislabeled rows may still exist) and the strict cleaned
    views consumed by the dashboard/report. It never updates or deletes data.
    """
    canonical = source_group.upper().strip()
    if canonical not in {"DAN", "DWT"}:
        raise ValueError("audit-estates hanya menerima DAN atau DWT")

    expected = "PU*" if canonical == "DAN" else "ME*"
    company_sql = """
        CASE
            WHEN upper(btrim(coalesce(source_group::text, '')))
                 IN ('DAN', 'ELHP_DAN') THEN 'DAN'
            WHEN upper(btrim(coalesce(source_group::text, '')))
                 IN ('DWT', 'ELHP_DWT') THEN 'DWT'
            ELSE upper(btrim(coalesce(source_company::text, '')))
        END
    """
    layers = [
        (
            "GROUP",
            "gold.reused_photo_group_summary",
            "gold.elhp_gt_group_summary_cleaned",
            "estate_list",
            "coalesce(usage_count, 0)::bigint",
        ),
        (
            "DETAIL",
            "gold.reused_photo_detail",
            "gold.elhp_gt_detail_transaction",
            "estate",
            "1::bigint",
        ),
        (
            "MONTHLY",
            "gold.reused_photo_dashboard_monthly",
            "gold.elhp_dashboard_monthly_cleaned",
            "estate",
            "coalesce(usage_count, 0)::bigint",
        ),
    ]

    results: list[dict[str, Any]] = []
    samples: list[dict[str, Any]] = []
    with connection(settings) as conn:
        for layer, base_table, cleaned_view, estate_column, weight_sql in layers:
            base_rows = fetch_all_dict(
                conn,
                f"""
                WITH scoped AS (
                    SELECT
                        {estate_column}::text AS estate_value,
                        {weight_sql} AS usage_weight
                    FROM {base_table}
                    WHERE is_active=true
                      AND ({company_sql})=%s
                )
                SELECT
                    count(*)::bigint AS rows,
                    coalesce(sum(usage_weight), 0)::bigint AS usages,
                    count(*) FILTER (
                        WHERE processing.elhp_estate_matches_company(
                            %s, estate_value
                        )
                    )::bigint AS valid_rows,
                    coalesce(sum(usage_weight) FILTER (
                        WHERE processing.elhp_estate_matches_company(
                            %s, estate_value
                        )
                    ), 0)::bigint AS valid_usages,
                    count(*) FILTER (
                        WHERE NOT processing.elhp_estate_matches_company(
                            %s, estate_value
                        )
                    )::bigint AS wrong_rows,
                    coalesce(sum(usage_weight) FILTER (
                        WHERE NOT processing.elhp_estate_matches_company(
                            %s, estate_value
                        )
                    ), 0)::bigint AS wrong_usages
                FROM scoped
                """,
                (canonical, canonical, canonical, canonical, canonical),
            )[0]
            cleaned_rows = fetch_all_dict(
                conn,
                f"""
                WITH scoped AS (
                    SELECT
                        {estate_column}::text AS estate_value,
                        {weight_sql} AS usage_weight
                    FROM {cleaned_view}
                    WHERE ({company_sql})=%s
                )
                SELECT
                    count(*)::bigint AS rows,
                    coalesce(sum(usage_weight), 0)::bigint AS usages,
                    count(*) FILTER (
                        WHERE NOT processing.elhp_estate_matches_company(
                            %s, estate_value
                        )
                    )::bigint AS wrong_rows,
                    coalesce(sum(usage_weight) FILTER (
                        WHERE NOT processing.elhp_estate_matches_company(
                            %s, estate_value
                        )
                    ), 0)::bigint AS wrong_usages
                FROM scoped
                """,
                (canonical, canonical, canonical),
            )[0]
            results.append(
                {
                    "layer": layer,
                    "base": dict(base_rows),
                    "cleaned": dict(cleaned_rows),
                }
            )

            if layer == "DETAIL":
                samples = fetch_all_dict(
                    conn,
                    f"""
                    WITH scoped AS (
                        SELECT
                            {estate_column}::text AS estate_value,
                            {weight_sql} AS usage_weight
                        FROM {base_table}
                        WHERE is_active=true
                          AND ({company_sql})=%s
                    )
                    SELECT
                        coalesce(nullif(btrim(estate_value), ''), '<KOSONG>')
                            AS estate,
                        count(*)::bigint AS rows,
                        coalesce(sum(usage_weight), 0)::bigint AS usages
                    FROM scoped
                    WHERE NOT processing.elhp_estate_matches_company(
                        %s, estate_value
                    )
                    GROUP BY estate_value
                    ORDER BY usages DESC, estate
                    LIMIT 20
                    """,
                    (canonical, canonical),
                )

    cleaned_usages = sum(
        int(item["cleaned"].get("usages") or 0)
        for item in results
        if item["layer"] == "DETAIL"
    )
    cleaned_wrong = sum(
        int(item["cleaned"].get("wrong_usages") or 0) for item in results
    )
    passed = cleaned_usages > 0 and cleaned_wrong == 0

    print("=" * 78)
    print(f"ELHP ESTATE OWNERSHIP AUDIT: {canonical} | EXPECTED={expected}")
    print("=" * 78)
    print("ACTIVE GOLD DASAR (read-only; salah label lama tetap disimpan untuk audit)")
    print(
        f"{'LAYER':<10} {'ROWS':>12} {'USAGES':>12} "
        f"{'VALID':>12} {'WRONG':>12}"
    )
    for item in results:
        base = item["base"]
        print(
            f"{item['layer']:<10} {int(base.get('rows') or 0):>12,} "
            f"{int(base.get('usages') or 0):>12,} "
            f"{int(base.get('valid_usages') or 0):>12,} "
            f"{int(base.get('wrong_usages') or 0):>12,}"
        )

    print("-" * 78)
    print("CLEANED VIEW YANG DIBACA DASHBOARD/REPORT")
    print(
        f"{'LAYER':<10} {'ROWS':>12} {'USAGES':>12} "
        f"{'WRONG ROW':>12} {'WRONG USE':>12}"
    )
    for item in results:
        cleaned = item["cleaned"]
        print(
            f"{item['layer']:<10} {int(cleaned.get('rows') or 0):>12,} "
            f"{int(cleaned.get('usages') or 0):>12,} "
            f"{int(cleaned.get('wrong_rows') or 0):>12,} "
            f"{int(cleaned.get('wrong_usages') or 0):>12,}"
        )

    if samples:
        print("-" * 78)
        print("CONTOH ESTATE SALAH DI GOLD DASAR (tidak tampil di dashboard/report)")
        for row in samples:
            print(
                f"  estate={row['estate']} rows={int(row['rows'] or 0):,} "
                f"usages={int(row['usages'] or 0):,}"
            )

    print("-" * 78)
    if passed:
        print(
            f"PASS {canonical} yang terlihat hanya estate {expected}; "
            "wrong usage pada cleaned view=0."
        )
    elif cleaned_usages == 0:
        print(
            f"FAIL cleaned view {canonical} kosong; periksa isi estate dan jalankan "
            "init-db kembali."
        )
    else:
        print(
            f"FAIL masih ada {cleaned_wrong:,} wrong usage pada cleaned view "
            f"{canonical}."
        )

    return {
        "canonical_source_group": canonical,
        "expected_estate": expected,
        "layers": results,
        "wrong_estate_samples": samples,
        "passed": passed,
    }


def audit_month_coverage(settings: Settings, source_group: str) -> dict[str, Any]:
    """Compare month availability from Silver through publishable dashboard data.

    DAN/DWT automatically include their historical ELHP_* aliases in the
    diagnostic. Context date and publishable Context (date plus estate) are
    deliberately separate. This command is read-only and makes it explicit
    whether a missing month is only a dashboard-selection issue or is absent
    before PUBLISH.
    """
    canonical, aliases = _month_audit_aliases(source_group)
    image_table = qname(settings.silver_schema, settings.image_table)
    with connection(settings) as conn:
        rows = fetch_all_dict(
            conn,
            f"""
            WITH silver_base AS (
                SELECT
                    upper(i.source_group::text) AS source_group,
                    i.file_uid::text AS file_uid,
                    upper(trim(i.file_name::text)) AS logical_name,
                    CASE
                        WHEN (regexp_match(i.file_name::text, '(20[0-9]{{4}})'))[1]
                             ~ '^20[0-9]{{2}}(0[1-9]|1[0-2])$'
                        THEN left((regexp_match(i.file_name::text, '(20[0-9]{{4}})'))[1], 4)
                             || '-'
                             || right((regexp_match(i.file_name::text, '(20[0-9]{{4}})'))[1], 2)
                        ELSE NULL
                    END AS filename_month,
                    c.period_date,
                    c.estate AS context_estate,
                    (i.image_bytes IS NOT NULL) AS has_source_bytea,
                    (bx.file_uid IS NOT NULL) AS bytea_corrupt_original,
                    (br.file_uid IS NOT NULL) AS bytea_recovered
                FROM {image_table} i
                LEFT JOIN processing.image_context c
                  ON c.file_uid=i.file_uid::text
                LEFT JOIN processing.source_bytea_exception bx
                  ON bx.source_group=upper(i.source_group::text)
                 AND bx.file_uid=i.file_uid::text
                 AND bx.is_active=true
                LEFT JOIN processing.gambar_reused br
                  ON br.source_group=upper(i.source_group::text)
                 AND br.file_uid=i.file_uid::text
                 AND br.is_active=true
                WHERE upper(i.source_group::text)=ANY(%s)
                  AND coalesce(i.file_exists, true)=true
                  AND coalesce(i.zero_byte, false)=false
                  AND coalesce(i.is_transport, false)=false
            ), feature_base AS (
                SELECT
                    upper(f.source_group) AS source_group,
                    f.file_uid,
                    upper(trim(f.file_name)) AS logical_name,
                    c.period_date,
                    c.estate AS context_estate
                FROM processing.image_feature f
                LEFT JOIN processing.image_context c ON c.file_uid=f.file_uid
                WHERE upper(f.source_group)=ANY(%s)
                  AND f.model_version=%s
                  AND f.is_valid=true
                  AND f.is_transport=false
            ), context_base AS (
                -- Bulan incremental baru sudah mempunyai Silver+Context tetapi
                -- belum mempunyai feature. Membaca Context hanya melalui
                -- image_feature membuat guard EMBED menjadi circular: EMBED
                -- menunggu Context, sementara audit Context menunggu EMBED.
                SELECT
                    source_group,
                    file_uid,
                    logical_name,
                    period_date,
                    context_estate
                FROM silver_base

                UNION ALL

                -- Histori yang BYTEA Silver-nya sudah dibersihkan tetap
                -- terlihat melalui feature metadata tanpa menggandakan file
                -- yang masih tersedia di Silver.
                SELECT
                    f.source_group,
                    f.file_uid,
                    f.logical_name,
                    f.period_date,
                    f.context_estate
                FROM feature_base f
                WHERE NOT EXISTS (
                    SELECT 1
                    FROM silver_base s
                    WHERE s.file_uid=f.file_uid
                )
            ), latest_validated_run AS (
                SELECT r.run_id
                FROM processing.pipeline_run r
                WHERE upper(r.source_group)=%s
                  AND EXISTS (
                      SELECT 1
                      FROM processing.validated_group_member vm
                      WHERE vm.run_id=r.run_id
                  )
                ORDER BY r.started_at DESC
                LIMIT 1
            ), layers AS (
                SELECT
                    'SILVER_FILENAME'::text AS layer,
                    source_group,
                    coalesce(filename_month, 'TANPA_BULAN') AS month,
                    count(DISTINCT logical_name)::bigint AS rows
                FROM silver_base
                GROUP BY source_group, coalesce(filename_month, 'TANPA_BULAN')

                UNION ALL

                SELECT
                    'BYTEA_EFFECTIVE'::text,
                    source_group,
                    coalesce(to_char(period_date, 'YYYY-MM'), filename_month, 'TANPA_BULAN'),
                    count(DISTINCT logical_name)::bigint
                FROM silver_base
                WHERE (has_source_bytea OR bytea_recovered)
                  AND (NOT bytea_corrupt_original OR bytea_recovered)
                GROUP BY source_group,
                         coalesce(to_char(period_date, 'YYYY-MM'), filename_month, 'TANPA_BULAN')

                UNION ALL

                SELECT
                    'BYTEA_PENDING_RECOVERY'::text,
                    source_group,
                    coalesce(to_char(period_date, 'YYYY-MM'), filename_month, 'TANPA_BULAN'),
                    count(DISTINCT logical_name)::bigint
                FROM silver_base
                WHERE bytea_corrupt_original AND NOT bytea_recovered
                GROUP BY source_group,
                         coalesce(to_char(period_date, 'YYYY-MM'), filename_month, 'TANPA_BULAN')

                UNION ALL

                SELECT
                    'CONTEXT_DATE'::text,
                    source_group,
                    coalesce(to_char(period_date, 'YYYY-MM'), 'TANPA_BULAN'),
                    count(DISTINCT logical_name)::bigint
                FROM context_base
                GROUP BY source_group, coalesce(to_char(period_date, 'YYYY-MM'), 'TANPA_BULAN')

                UNION ALL

                SELECT
                    'CONTEXT_PUBLISHABLE'::text,
                    source_group,
                    coalesce(to_char(period_date, 'YYYY-MM'), 'TANPA_BULAN'),
                    count(DISTINCT logical_name)::bigint
                FROM context_base
                WHERE period_date IS NOT NULL
                  AND nullif(btrim(context_estate::text), '') IS NOT NULL
                GROUP BY source_group, coalesce(to_char(period_date, 'YYYY-MM'), 'TANPA_BULAN')

                UNION ALL

                SELECT
                    'EMBED_VALID'::text,
                    source_group,
                    coalesce(to_char(period_date, 'YYYY-MM'), 'TANPA_BULAN'),
                    count(DISTINCT logical_name)::bigint
                FROM feature_base
                GROUP BY source_group, coalesce(to_char(period_date, 'YYYY-MM'), 'TANPA_BULAN')

                UNION ALL

                SELECT
                    'VALIDATED_LATEST'::text,
                    %s::text,
                    coalesce(to_char(c.period_date, 'YYYY-MM'), 'TANPA_BULAN'),
                    count(DISTINCT vm.file_uid)::bigint
                FROM processing.validated_group_member vm
                JOIN latest_validated_run lr ON lr.run_id=vm.run_id
                LEFT JOIN processing.image_context c ON c.file_uid=vm.file_uid
                GROUP BY coalesce(to_char(c.period_date, 'YYYY-MM'), 'TANPA_BULAN')

                UNION ALL

                SELECT
                    'GOLD_DETAIL'::text,
                    upper(source_group),
                    coalesce(to_char(tanggal, 'YYYY-MM'), 'TANPA_BULAN'),
                    count(*)::bigint
                FROM gold.reused_photo_detail
                WHERE upper(source_group)=ANY(%s)
                  AND is_active=true
                GROUP BY upper(source_group), coalesce(to_char(tanggal, 'YYYY-MM'), 'TANPA_BULAN')

                UNION ALL

                SELECT
                    'DASHBOARD_MONTHLY'::text,
                    upper(source_group),
                    to_char(month, 'YYYY-MM'),
                    sum(usage_count)::bigint
                FROM gold.reused_photo_dashboard_monthly
                WHERE upper(source_group)=ANY(%s)
                  AND is_active=true
                GROUP BY upper(source_group), to_char(month, 'YYYY-MM')
            )
            SELECT layer, source_group, month, rows
            FROM layers
            ORDER BY
                CASE layer
                    WHEN 'SILVER_FILENAME' THEN 1
                    WHEN 'BYTEA_EFFECTIVE' THEN 2
                    WHEN 'BYTEA_PENDING_RECOVERY' THEN 3
                    WHEN 'CONTEXT_DATE' THEN 4
                    WHEN 'CONTEXT_PUBLISHABLE' THEN 5
                    WHEN 'EMBED_VALID' THEN 6
                    WHEN 'VALIDATED_LATEST' THEN 7
                    WHEN 'GOLD_DETAIL' THEN 8
                    ELSE 9
                END,
                source_group,
                month
            """,
            (
                aliases,
                aliases,
                settings.model_version,
                canonical,
                canonical,
                aliases,
                aliases,
            ),
        )
        context_sources = fetch_all_dict(
            conn,
            f"""
            WITH context_files AS (
                SELECT
                    upper(i.source_group::text) AS source_group,
                    i.file_uid::text AS file_uid
                FROM {image_table} i
                WHERE upper(i.source_group::text)=ANY(%s)
                  AND coalesce(i.file_exists, true)=true
                  AND coalesce(i.zero_byte, false)=false
                  AND coalesce(i.is_transport, false)=false

                UNION ALL

                SELECT
                    upper(f.source_group::text) AS source_group,
                    f.file_uid::text AS file_uid
                FROM processing.image_feature f
                WHERE upper(f.source_group)=ANY(%s)
                  AND f.model_version=%s
                  AND f.is_valid=true
                  AND f.is_transport=false
                  AND NOT EXISTS (
                      SELECT 1
                      FROM {image_table} i
                      WHERE i.file_uid::text=f.file_uid
                  )
            )
            SELECT
                f.source_group,
                coalesce(to_char(c.period_date, 'YYYY-MM'), 'TANPA_BULAN') AS month,
                coalesce(nullif(btrim(c.match_method), ''), 'TANPA_CONTEXT') AS match_method,
                count(DISTINCT f.file_uid::text)::bigint AS rows,
                count(DISTINCT f.file_uid::text) FILTER (
                    WHERE c.period_date IS NOT NULL
                      AND nullif(btrim(c.estate), '') IS NOT NULL
                )::bigint AS publishable_rows
            FROM context_files f
            LEFT JOIN processing.image_context c
              ON c.file_uid=f.file_uid
            GROUP BY
                f.source_group,
                coalesce(to_char(c.period_date, 'YYYY-MM'), 'TANPA_BULAN'),
                coalesce(nullif(btrim(c.match_method), ''), 'TANPA_CONTEXT')
            ORDER BY source_group, month, rows DESC, match_method
            """,
            (aliases, aliases, settings.model_version),
        )
        historical_gold_rows = fetch_all_dict(
            conn,
            """
            SELECT
                to_char(tanggal, 'YYYY-MM') AS month,
                count(*) FILTER (WHERE is_active=true)::bigint AS active_usages,
                count(*)::bigint AS version_rows,
                count(DISTINCT run_id)::bigint AS runs
            FROM gold.reused_photo_detail
            WHERE upper(source_group)=ANY(%s)
              AND tanggal IS NOT NULL
            GROUP BY to_char(tanggal, 'YYYY-MM')
            ORDER BY month
            """,
            (aliases,),
        )

    print("=" * 78)
    print(
        f"MONTH COVERAGE AUDIT: {canonical} | "
        f"aliases={','.join(aliases)}"
    )
    print("=" * 78)
    print(f"{'LAYER':<20} {'SOURCE':<12} {'MONTH':<14} {'ROWS':>14}")
    print("-" * 78)
    for row in rows:
        print(
            f"{str(row['layer']):<20} {str(row['source_group']):<12} "
            f"{str(row['month']):<14} {int(row['rows'] or 0):>14,}"
        )

    print("-" * 78)
    print("CONTEXT LINEAGE (asal transaksi per bulan)")
    print(
        f"{'SOURCE':<12} {'MONTH':<14} {'MATCH METHOD':<36} "
        f"{'ROWS':>10} {'PUBLISHABLE':>12}"
    )
    for row in context_sources:
        print(
            f"{str(row['source_group']):<12} {str(row['month']):<14} "
            f"{str(row['match_method']):<36} "
            f"{int(row['rows'] or 0):>10,} "
            f"{int(row['publishable_rows'] or 0):>12,}"
        )

    print("-" * 78)
    print("GOLD HISTORY REFERENCE (termasuk snapshot nonaktif)")
    print(f"{'MONTH':<14} {'ACTIVE USAGE':>14} {'VERSION ROWS':>14} {'RUNS':>8}")
    for row in historical_gold_rows:
        print(
            f"{str(row['month']):<14} "
            f"{int(row['active_usages'] or 0):>14,} "
            f"{int(row['version_rows'] or 0):>14,} "
            f"{int(row['runs'] or 0):>8,}"
        )

    month_sets: dict[str, set[str]] = {}
    for row in rows:
        month = str(row.get("month") or "")
        if month == "TANPA_BULAN":
            continue
        month_sets.setdefault(str(row["layer"]), set()).add(month)
    silver_months = month_sets.get("SILVER_FILENAME", set())
    source_months = (
        silver_months
        | month_sets.get("CONTEXT_DATE", set())
        | month_sets.get("EMBED_VALID", set())
    )
    gold_months = month_sets.get("GOLD_DETAIL", set())
    embedding_months = month_sets.get("EMBED_VALID", set())
    publishable_context_months = month_sets.get("CONTEXT_PUBLISHABLE", set())
    validated_months = month_sets.get("VALIDATED_LATEST", set())
    dashboard_months = month_sets.get("DASHBOARD_MONTHLY", set())
    historical_gold_months = {
        str(row.get("month") or "")
        for row in historical_gold_rows
        if str(row.get("month") or "")
    }
    historical_missing_source = sorted(historical_gold_months - source_months)
    historical_missing_active = sorted(historical_gold_months - gold_months)
    historical_missing_silver = sorted(historical_gold_months - silver_months)
    missing_embedding = sorted(source_months - embedding_months)
    missing_publishable_context = sorted(source_months - publishable_context_months)
    missing_validated = sorted(source_months - validated_months)
    missing_gold = sorted(source_months - gold_months)
    missing_dashboard = sorted(gold_months - dashboard_months)
    pending_recovery_rows = sum(
        int(row.get("rows") or 0)
        for row in rows
        if row.get("layer") == "BYTEA_PENDING_RECOVERY"
    )
    if missing_gold:
        print(
            "WARN bulan ada di source/context tetapi belum ada di Gold: "
            + ", ".join(missing_gold)
        )
    if historical_missing_source:
        print(
            "FAIL bulan pernah tersedia di Gold tetapi sekarang hilang dari "
            "Embedding/Context: " + ", ".join(historical_missing_source)
        )
        print(
            "ACTION pulihkan metadata/embedding bulan historis sebelum MATCH. "
            "BYTEA penuh tidak perlu dimuat ke Silver."
        )
    elif historical_missing_silver:
        print(
            "INFO BYTEA Silver historis tidak tersedia untuk bulan: "
            + ", ".join(historical_missing_silver)
            + ". Embedding+Context tetap tersedia; gunakan merge-month dengan "
              "--source-root agar VALIDATE membaca candidate langsung dari NAS."
        )
    if historical_missing_active:
        print(
            "FAIL bulan pada riwayat Gold tidak ada di snapshot Gold aktif: "
            + ", ".join(historical_missing_active)
        )
        print(
            "ACTION aktifkan kembali snapshot lengkap dengan restore_gold_run.py "
            "sambil menyiapkan rebuild Januari sampai bulan terbaru."
        )
    if missing_embedding:
        print("WARN bulan belum mempunyai embedding valid: " + ", ".join(missing_embedding))
    if missing_publishable_context:
        print(
            "WARN bulan belum mempunyai Context publishable (tanggal+estate): "
            + ", ".join(missing_publishable_context)
        )
    if missing_validated:
        print("WARN bulan belum masuk validated run terbaru: " + ", ".join(missing_validated))
    if pending_recovery_rows:
        print(
            f"WARN BYTEA belum recovery efektif: {pending_recovery_rows:,} image. "
            f"Jalankan recover_corrupt_bytea.py --source-group {canonical} "
            "dengan root NAS yang benar, lalu gunakan audit-source untuk verifikasi."
        )
    if missing_publishable_context:
        print(
            f"ACTION lengkapi business context: py run_pipeline.py sync-raw-context "
            f"--source-group {canonical}"
        )
    if missing_embedding or missing_validated or missing_gold:
        print(
            f"ACTION buat snapshot baru: py run_pipeline.py rebuild-company "
            f"--source-group {canonical}"
        )
    if missing_dashboard:
        print(
            "WARN bulan ada di Gold tetapi belum ada di aggregate dashboard: "
            + ", ".join(missing_dashboard)
        )
        print("ACTION jalankan: py run_pipeline.py init-db")
    if (
        not missing_gold
        and not missing_dashboard
        and not historical_missing_source
        and not historical_missing_active
    ):
        print("OK month coverage konsisten dari source/context sampai dashboard.")
    alias_rows = sum(
        int(row.get("rows") or 0)
        for row in rows
        if row.get("layer") == "SILVER_FILENAME"
        and str(row.get("source_group")) != canonical
    )
    if alias_rows:
        print(
            f"WARN masih ada {alias_rows:,} logical image aktif pada alias "
            f"{', '.join(alias for alias in aliases if alias != canonical)}. "
            "Pipeline canonical tidak membacanya sebelum source disatukan."
        )
    return {
        "canonical_source_group": canonical,
        "aliases": aliases,
        "rows": rows,
        "context_sources": context_sources,
        "source_months": sorted(source_months),
        "embedding_months": sorted(embedding_months),
        "publishable_context_months": sorted(publishable_context_months),
        "validated_months": sorted(validated_months),
        "gold_months": sorted(gold_months),
        "dashboard_months": sorted(dashboard_months),
        "historical_gold_months": sorted(historical_gold_months),
        "historical_months_missing_source": historical_missing_source,
        "historical_months_missing_active": historical_missing_active,
        "historical_months_missing_silver": historical_missing_silver,
        "missing_gold_months": missing_gold,
        "missing_embedding_months": missing_embedding,
        "missing_publishable_context_months": missing_publishable_context,
        "missing_validated_months": missing_validated,
        "missing_dashboard_months": missing_dashboard,
        "bytea_pending_recovery_rows": pending_recovery_rows,
        "active_alias_logical_images": alias_rows,
    }


def _audit_raw_oph_date_evidence(
    conn,
    settings: Settings,
    *,
    canonical: str,
    aliases: list[str],
    target_run: str,
    source_cte: str,
) -> dict[str, Any]:
    """Cross-check Gold OPH against the RAW production business date.

    Filename/Context/Gold agreement alone can repeat the same wrong value.  This
    query supplies an independent fourth leg: normalized OPH -> RAW production
    date.  A date is only called proven when that OPH has one distinct RAW date
    and all four dates agree exactly.
    """
    raw_schema = settings.raw_business_schema
    raw_table_name = settings.raw_business_table
    raw_table = qname(raw_schema, raw_table_name)
    if not table_exists(conn, raw_schema, raw_table_name):
        return {
            "available": False,
            "warning": f"tabel RAW tidak ditemukan: {raw_table}",
            "summary": [],
            "volume": [],
        }

    columns = table_columns(conn, raw_schema, raw_table_name)
    oph_col = _find_business_column(columns, BUSINESS_ALIASES["oph"])
    company_col = _find_business_column(
        columns, BUSINESS_ALIASES["company_code"]
    )
    account_status_col = _find_business_column(
        columns, BUSINESS_ALIASES["account_status"]
    )
    raw_date_sql = _period_date_sql(columns, alias="p")
    if not oph_col:
        return {
            "available": False,
            "warning": (
                f"kolom OPH tidak ditemukan di {raw_table}; kandidat="
                + ",".join(BUSINESS_ALIASES["oph"])
            ),
            "summary": [],
            "volume": [],
        }
    if raw_date_sql == "NULL::date":
        return {
            "available": False,
            "warning": (
                f"kolom tanggal bisnis tidak ditemukan di {raw_table}; kandidat="
                + ",".join(BUSINESS_ALIASES["period_date"])
                + ",period_year+period_month+period_day"
            ),
            "summary": [],
            "volume": [],
        }

    raw_filters = [
        f"nullif(btrim(p.{ident(oph_col)}::text), '') IS NOT NULL"
    ]
    raw_params: list[object] = []
    scope_parts: list[str] = []
    if company_col:
        raw_filters.append(
            f"upper(btrim(p.{ident(company_col)}::text))=upper(%s)"
        )
        raw_params.append(settings.source_company(canonical))
        scope_parts.append(
            f"{company_col}={settings.source_company(canonical)}"
        )
    else:
        scope_parts.append("company=UNSCOPED(kolom tidak ditemukan)")
    status_values = settings.account_status_values_for(canonical)
    if account_status_col and status_values:
        raw_filters.append(
            f"upper(btrim(p.{ident(account_status_col)}::text))=ANY(%s)"
        )
        raw_params.append(list(status_values))
        scope_parts.append(
            f"{account_status_col}="
            + "|".join(status_values)
        )
    elif account_status_col:
        scope_parts.append(f"{account_status_col}=ALL")

    query = source_cte + f"""
        , gold_scope AS MATERIALIZED (
            SELECT
                d.group_id,
                d.file_uid,
                d.image_name,
                d.tanggal AS gold_date,
                sf.filename_date,
                c.period_date AS context_date,
                coalesce(
                    nullif(btrim(c.oph_key), ''),
                    nullif(processing.normalize_oph(d.oph), '')
                ) AS oph_key
            FROM gold.reused_photo_detail d
            LEFT JOIN source_file sf ON sf.file_uid=d.file_uid
            LEFT JOIN processing.image_context c ON c.file_uid=d.file_uid
            WHERE upper(d.source_group)=upper(%s)
              AND d.run_id=%s::uuid
        ), requested_oph AS MATERIALIZED (
            SELECT DISTINCT oph_key
            FROM gold_scope
            WHERE nullif(btrim(oph_key), '') IS NOT NULL
        ), raw_rows AS MATERIALIZED (
            SELECT
                processing.normalize_oph(p.{ident(oph_col)}::text) AS oph_key,
                {raw_date_sql} AS raw_date
            FROM {raw_table} p
            JOIN requested_oph r
              ON r.oph_key=processing.normalize_oph(p.{ident(oph_col)}::text)
            WHERE {' AND '.join(raw_filters)}
        ), raw_dates AS MATERIALIZED (
            SELECT
                oph_key,
                count(*)::bigint AS raw_rows,
                count(DISTINCT raw_date)
                    FILTER (WHERE raw_date IS NOT NULL) AS raw_date_count,
                min(raw_date) AS raw_min_date,
                max(raw_date) AS raw_max_date,
                array_agg(DISTINCT raw_date ORDER BY raw_date)
                    FILTER (WHERE raw_date IS NOT NULL) AS raw_dates
            FROM raw_rows
            GROUP BY oph_key
        ), comparison AS MATERIALIZED (
            SELECT
                g.*,
                rd.raw_rows,
                rd.raw_date_count,
                rd.raw_min_date,
                rd.raw_max_date,
                CASE
                    WHEN nullif(btrim(g.oph_key), '') IS NULL
                    THEN 'CONTEXT_OPH_MISSING'
                    WHEN rd.oph_key IS NULL
                    THEN 'RAW_OPH_NOT_FOUND'
                    WHEN coalesce(rd.raw_date_count, 0)=0
                    THEN 'RAW_DATE_MISSING'
                    WHEN g.filename_date IS NULL
                      OR g.context_date IS NULL
                      OR g.gold_date IS NULL
                    THEN 'LINEAGE_DATE_MISSING'
                    WHEN NOT (
                        g.filename_date=g.context_date
                        AND g.context_date=g.gold_date
                    )
                    THEN 'LINEAGE_CONFLICT_BEFORE_RAW'
                    WHEN rd.raw_date_count=1
                     AND g.filename_date=ANY(rd.raw_dates)
                    THEN 'PROVEN_EXACT_DAY'
                    WHEN rd.raw_date_count>1
                     AND g.filename_date=ANY(rd.raw_dates)
                    THEN 'RAW_AMBIGUOUS_DATES_INCLUDES_EXACT'
                    WHEN rd.raw_date_count=1
                     AND EXISTS (
                         SELECT 1 FROM unnest(rd.raw_dates) AS x(raw_date)
                         WHERE date_trunc('month', x.raw_date)=
                               date_trunc('month', g.filename_date)
                     )
                    THEN 'RAW_SAME_MONTH_DIFFERENT_DAY'
                    WHEN rd.raw_date_count>1
                     AND EXISTS (
                         SELECT 1 FROM unnest(rd.raw_dates) AS x(raw_date)
                         WHERE date_trunc('month', x.raw_date)=
                               date_trunc('month', g.filename_date)
                     )
                    THEN 'RAW_AMBIGUOUS_DATES_SAME_MONTH'
                    ELSE 'RAW_MISMATCH_MONTH'
                END AS raw_date_status
            FROM gold_scope g
            LEFT JOIN raw_dates rd ON rd.oph_key=g.oph_key
        ), per_oph AS (
            SELECT
                coalesce(to_char(gold_date, 'YYYY-MM'), 'TANPA_GOLD_DATE')
                    AS gold_month,
                oph_key,
                count(DISTINCT group_id)::bigint AS groups,
                count(*)::bigint AS usages
            FROM comparison
            WHERE nullif(btrim(oph_key), '') IS NOT NULL
            GROUP BY
                coalesce(to_char(gold_date, 'YYYY-MM'), 'TANPA_GOLD_DATE'),
                oph_key
        ), month_totals AS (
            SELECT
                coalesce(to_char(gold_date, 'YYYY-MM'), 'TANPA_GOLD_DATE')
                    AS gold_month,
                count(DISTINCT group_id)::bigint AS groups,
                count(*)::bigint AS usages
            FROM comparison
            GROUP BY
                coalesce(to_char(gold_date, 'YYYY-MM'), 'TANPA_GOLD_DATE')
        )
        SELECT
            'STATUS'::text AS record_kind,
            coalesce(to_char(gold_date, 'YYYY-MM'), 'TANPA_GOLD_DATE')
                AS gold_month,
            raw_date_status AS status,
            count(DISTINCT group_id)::bigint AS groups,
            count(*)::bigint AS usages,
            count(DISTINCT oph_key)::bigint AS distinct_oph,
            NULL::numeric AS avg_usages_per_oph,
            NULL::numeric AS p95_usages_per_oph,
            NULL::bigint AS max_usages_per_oph
        FROM comparison
        GROUP BY
            coalesce(to_char(gold_date, 'YYYY-MM'), 'TANPA_GOLD_DATE'),
            raw_date_status
        UNION ALL
        SELECT
            'OPH_VOLUME'::text AS record_kind,
            p.gold_month,
            'ALL_OPH'::text AS status,
            mt.groups,
            mt.usages,
            count(*)::bigint AS distinct_oph,
            round(avg(p.usages), 2) AS avg_usages_per_oph,
            round(
                (percentile_cont(0.95) WITHIN GROUP (ORDER BY p.usages))::numeric,
                2
            )
                AS p95_usages_per_oph,
            max(p.usages)::bigint AS max_usages_per_oph
        FROM per_oph p
        JOIN month_totals mt ON mt.gold_month=p.gold_month
        GROUP BY p.gold_month, mt.groups, mt.usages
        ORDER BY gold_month, record_kind, status
    """
    rows = fetch_all_dict(
        conn,
        query,
        (aliases, canonical, canonical, target_run, *raw_params),
    )
    return {
        "available": True,
        "warning": None,
        "raw_table": f"{raw_schema}.{raw_table_name}",
        "raw_oph_column": oph_col,
        "scope": "; ".join(scope_parts),
        "summary": [row for row in rows if row["record_kind"] == "STATUS"],
        "volume": [row for row in rows if row["record_kind"] == "OPH_VOLUME"],
    }


def audit_date_lineage(
    settings: Settings,
    source_group: str,
    *,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Audit asal tanggal tanpa membaca BYTEA atau mengubah data.

    Tanggal pada filename, Context, dan Gold dibandingkan pada level hari dan
    bulan. Audit ini membedakan ledakan data yang memang sudah ada pada nama
    file dari pergeseran bulan akibat pemilihan transaksi/fallback Context.
    """
    canonical, aliases = _month_audit_aliases(source_group)
    image_table = qname(settings.silver_schema, settings.image_table)

    with connection(settings) as conn:
        if run_id:
            target = fetch_all_dict(
                conn,
                """
                SELECT run_id::text AS run_id, source_group, stage, status
                FROM processing.pipeline_run
                WHERE run_id=%s::uuid
                """,
                (run_id,),
            )
        else:
            target = fetch_all_dict(
                conn,
                """
                SELECT r.run_id::text AS run_id, r.source_group, r.stage, r.status
                FROM processing.pipeline_run r
                WHERE upper(r.source_group)=upper(%s)
                  AND EXISTS (
                      SELECT 1
                      FROM gold.reused_photo_group g
                      WHERE g.run_id=r.run_id AND g.is_active=true
                  )
                ORDER BY r.started_at DESC
                LIMIT 1
                """,
                (canonical,),
            )
        if not target:
            raise RuntimeError(
                f"Run Gold tidak ditemukan untuk {canonical}"
                + (f": {run_id}" if run_id else "")
            )
        target_run = str(target[0]["run_id"])
        target_source = str(target[0]["source_group"]).upper()
        if target_source != canonical:
            raise RuntimeError(
                f"run_id adalah source_group={target_source}, bukan {canonical}"
            )

        source_cte = f"""
            WITH source_token AS (
                SELECT DISTINCT ON (i.file_uid::text)
                    i.file_uid::text AS file_uid,
                    i.file_name::text AS file_name,
                    (regexp_match(i.file_name::text, '(20[0-9]{{6}})'))[1]
                        AS filename_date_token
                FROM {image_table} i
                WHERE upper(i.source_group::text)=ANY(%s)
                  AND coalesce(i.is_transport, false)=false
                  AND coalesce(i.file_exists, true)=true
                  AND coalesce(i.zero_byte, false)=false
                ORDER BY i.file_uid::text,
                         (upper(i.source_group::text)=%s) DESC
            ), source_file AS (
                SELECT
                    file_uid,
                    file_name,
                    CASE
                        WHEN filename_date_token ~ '^20[0-9]{{6}}$'
                         AND to_char(
                               to_date(filename_date_token, 'YYYYMMDD'),
                               'YYYYMMDD'
                             )=filename_date_token
                        THEN to_date(filename_date_token, 'YYYYMMDD')
                        ELSE NULL::date
                    END AS filename_date
                FROM source_token
            )
        """

        source_summary = fetch_all_dict(
            conn,
            source_cte
            + """
            SELECT
                coalesce(to_char(c.period_date, 'YYYY-MM'), 'TANPA_CONTEXT')
                    AS context_month,
                CASE
                    WHEN c.period_date IS NULL THEN 'CONTEXT_DATE_MISSING'
                    WHEN sf.filename_date IS NULL THEN 'FILENAME_DATE_MISSING'
                    WHEN c.period_date=sf.filename_date THEN 'EXACT_DAY'
                    WHEN date_trunc('month', c.period_date)=
                         date_trunc('month', sf.filename_date)
                    THEN 'SAME_MONTH_DIFFERENT_DAY'
                    ELSE 'MISMATCH_MONTH'
                END AS date_status,
                count(*)::bigint AS rows
            FROM source_file sf
            LEFT JOIN processing.image_context c ON c.file_uid=sf.file_uid
            GROUP BY
                coalesce(to_char(c.period_date, 'YYYY-MM'), 'TANPA_CONTEXT'),
                CASE
                    WHEN c.period_date IS NULL THEN 'CONTEXT_DATE_MISSING'
                    WHEN sf.filename_date IS NULL THEN 'FILENAME_DATE_MISSING'
                    WHEN c.period_date=sf.filename_date THEN 'EXACT_DAY'
                    WHEN date_trunc('month', c.period_date)=
                         date_trunc('month', sf.filename_date)
                    THEN 'SAME_MONTH_DIFFERENT_DAY'
                    ELSE 'MISMATCH_MONTH'
                END
            ORDER BY context_month, date_status
            """,
            (aliases, canonical),
        )

        source_mismatch = fetch_all_dict(
            conn,
            source_cte
            + """
            SELECT
                to_char(c.period_date, 'YYYY-MM') AS context_month,
                to_char(sf.filename_date, 'YYYY-MM') AS filename_month,
                coalesce(nullif(btrim(c.match_method), ''), 'TANPA_CONTEXT')
                    AS match_method,
                count(*)::bigint AS rows
            FROM source_file sf
            JOIN processing.image_context c ON c.file_uid=sf.file_uid
            WHERE c.period_date IS NOT NULL
              AND sf.filename_date IS NOT NULL
              AND date_trunc('month', c.period_date)<>
                  date_trunc('month', sf.filename_date)
            GROUP BY
                to_char(c.period_date, 'YYYY-MM'),
                to_char(sf.filename_date, 'YYYY-MM'),
                coalesce(nullif(btrim(c.match_method), ''), 'TANPA_CONTEXT')
            ORDER BY rows DESC, context_month, filename_month
            """,
            (aliases, canonical),
        )

        gold_summary = fetch_all_dict(
            conn,
            source_cte
            + """
            , gold_scope AS (
                SELECT
                    d.group_id,
                    d.file_uid,
                    d.image_name,
                    d.tanggal AS gold_date,
                    sf.filename_date,
                    c.period_date AS context_date,
                    coalesce(
                        nullif(btrim(d.match_source), ''),
                        nullif(btrim(c.match_method), ''),
                        'TANPA_CONTEXT'
                    ) AS match_method
                FROM gold.reused_photo_detail d
                LEFT JOIN source_file sf ON sf.file_uid=d.file_uid
                LEFT JOIN processing.image_context c ON c.file_uid=d.file_uid
                WHERE upper(d.source_group)=upper(%s)
                  AND d.run_id=%s::uuid
            )
            SELECT
                coalesce(to_char(gold_date, 'YYYY-MM'), 'TANPA_GOLD_DATE')
                    AS gold_month,
                CASE
                    WHEN gold_date IS NULL THEN 'GOLD_DATE_MISSING'
                    WHEN filename_date IS NULL THEN 'FILENAME_DATE_MISSING'
                    WHEN gold_date=filename_date THEN 'EXACT_DAY'
                    WHEN date_trunc('month', gold_date)=
                         date_trunc('month', filename_date)
                    THEN 'SAME_MONTH_DIFFERENT_DAY'
                    ELSE 'MISMATCH_MONTH'
                END AS date_status,
                count(DISTINCT group_id)::bigint AS groups,
                count(*)::bigint AS usages
            FROM gold_scope
            GROUP BY
                coalesce(to_char(gold_date, 'YYYY-MM'), 'TANPA_GOLD_DATE'),
                CASE
                    WHEN gold_date IS NULL THEN 'GOLD_DATE_MISSING'
                    WHEN filename_date IS NULL THEN 'FILENAME_DATE_MISSING'
                    WHEN gold_date=filename_date THEN 'EXACT_DAY'
                    WHEN date_trunc('month', gold_date)=
                         date_trunc('month', filename_date)
                    THEN 'SAME_MONTH_DIFFERENT_DAY'
                    ELSE 'MISMATCH_MONTH'
                END
            ORDER BY gold_month, date_status
            """,
            (aliases, canonical, canonical, target_run),
        )

        gold_mismatch = fetch_all_dict(
            conn,
            source_cte
            + """
            SELECT
                d.group_id,
                d.file_uid,
                d.image_name,
                d.tanggal AS gold_date,
                sf.filename_date,
                c.period_date AS context_date,
                coalesce(
                    nullif(btrim(d.match_source), ''),
                    nullif(btrim(c.match_method), ''),
                    'TANPA_CONTEXT'
                ) AS match_method,
                c.source_table,
                c.source_pk
            FROM gold.reused_photo_detail d
            LEFT JOIN source_file sf ON sf.file_uid=d.file_uid
            LEFT JOIN processing.image_context c ON c.file_uid=d.file_uid
            WHERE upper(d.source_group)=upper(%s)
              AND d.run_id=%s::uuid
              AND d.tanggal IS NOT NULL
              AND sf.filename_date IS NOT NULL
              AND date_trunc('month', d.tanggal)<>
                  date_trunc('month', sf.filename_date)
            ORDER BY d.tanggal, d.group_id, d.image_name
            LIMIT 25
            """,
            (aliases, canonical, canonical, target_run),
        )

        raw_oph_audit = _audit_raw_oph_date_evidence(
            conn,
            settings,
            canonical=canonical,
            aliases=aliases,
            target_run=target_run,
            source_cte=source_cte,
        )

    source_mismatch_rows = sum(int(row.get("rows") or 0) for row in source_mismatch)
    gold_mismatch_rows = sum(
        int(row.get("usages") or 0)
        for row in gold_summary
        if row.get("date_status") == "MISMATCH_MONTH"
    )
    gold_rows = sum(int(row.get("usages") or 0) for row in gold_summary)
    raw_summary = list(raw_oph_audit.get("summary") or [])
    raw_proven_rows = sum(
        int(row.get("usages") or 0)
        for row in raw_summary
        if row.get("status") == "PROVEN_EXACT_DAY"
    )
    raw_mismatch_rows = sum(
        int(row.get("usages") or 0)
        for row in raw_summary
        if row.get("status") == "RAW_MISMATCH_MONTH"
    )
    raw_uncertain_rows = max(0, gold_rows - raw_proven_rows - raw_mismatch_rows)

    print("=" * 78)
    print(f"DATE LINEAGE AUDIT: {canonical} | run_id={target_run}")
    print("=" * 78)
    print("SOURCE/CONTEXT DATE CONSISTENCY (tidak membaca BYTEA)")
    print(f"{'CONTEXT MONTH':<16} {'STATUS':<28} {'ROWS':>12}")
    print("-" * 62)
    for row in source_summary:
        print(
            f"{str(row['context_month']):<16} "
            f"{str(row['date_status']):<28} "
            f"{int(row['rows'] or 0):>12,}"
        )

    print("-" * 78)
    print("ACTIVE/TARGET GOLD DATE CONSISTENCY")
    print(f"{'GOLD MONTH':<16} {'STATUS':<28} {'GROUPS':>10} {'USAGES':>10}")
    for row in gold_summary:
        print(
            f"{str(row['gold_month']):<16} "
            f"{str(row['date_status']):<28} "
            f"{int(row['groups'] or 0):>10,} "
            f"{int(row['usages'] or 0):>10,}"
        )

    if source_mismatch:
        print("-" * 78)
        print("SOURCE MONTH MISMATCH MATRIX")
        print(
            f"{'CONTEXT':<10} {'FILENAME':<10} {'MATCH METHOD':<38} {'ROWS':>10}"
        )
        for row in source_mismatch:
            print(
                f"{str(row['context_month']):<10} "
                f"{str(row['filename_month']):<10} "
                f"{str(row['match_method']):<38} "
                f"{int(row['rows'] or 0):>10,}"
            )

    if gold_mismatch:
        print("-" * 78)
        print("GOLD MISMATCH SAMPLES (maksimum 25)")
        for row in gold_mismatch:
            print(
                f"  G{row['group_id']} gold={row['gold_date']} "
                f"filename={row['filename_date']} context={row['context_date']} "
                f"method={row['match_method']} file={row['image_name']}"
            )

    print("-" * 78)
    print("RAW PRODUCTION OPH/DATE CROSS-CHECK (independent business proof)")
    if not raw_oph_audit.get("available"):
        print(f"WARN {raw_oph_audit.get('warning')}")
    else:
        print(
            f"RAW={raw_oph_audit['raw_table']} "
            f"OPH_COLUMN={raw_oph_audit['raw_oph_column']} "
            f"SCOPE={raw_oph_audit['scope']}"
        )
        print(
            f"{'GOLD MONTH':<12} {'STATUS':<39} {'GROUPS':>9} "
            f"{'USAGES':>9} {'OPH':>9}"
        )
        for row in raw_summary:
            print(
                f"{str(row['gold_month']):<12} "
                f"{str(row['status']):<39} "
                f"{int(row['groups'] or 0):>9,} "
                f"{int(row['usages'] or 0):>9,} "
                f"{int(row['distinct_oph'] or 0):>9,}"
            )

        print("-" * 78)
        print("GOLD USAGE / OPH MULTIPLICITY")
        print(
            f"{'MONTH':<10} {'GROUPS':>9} {'USAGES':>9} {'DIST.OPH':>10} "
            f"{'AVG/OPH':>9} {'P95/OPH':>9} {'MAX/OPH':>9}"
        )
        for row in raw_oph_audit.get("volume") or []:
            print(
                f"{str(row['gold_month']):<10} "
                f"{int(row['groups'] or 0):>9,} "
                f"{int(row['usages'] or 0):>9,} "
                f"{int(row['distinct_oph'] or 0):>10,} "
                f"{float(row['avg_usages_per_oph'] or 0):>9.2f} "
                f"{float(row['p95_usages_per_oph'] or 0):>9.2f} "
                f"{int(row['max_usages_per_oph'] or 0):>9,}"
            )

    print("-" * 78)
    if gold_mismatch_rows:
        mismatch_pct = 100.0 * gold_mismatch_rows / gold_rows if gold_rows else 0.0
        print(
            f"FAIL Gold mempunyai {gold_mismatch_rows:,}/{gold_rows:,} usage "
            f"({mismatch_pct:.2f}%) dengan bulan berbeda dari filename."
        )
        print("ACTION jangan publish/rebuild; perbaiki pemetaan Context terlebih dahulu.")
    elif raw_oph_audit.get("available") and raw_mismatch_rows:
        mismatch_pct = 100.0 * raw_mismatch_rows / gold_rows if gold_rows else 0.0
        print(
            f"FAIL {raw_mismatch_rows:,}/{gold_rows:,} usage ({mismatch_pct:.2f}%) "
            "berada pada bulan berbeda dari tanggal OPH di RAW production."
        )
        print("ACTION jangan rebuild/publish; periksa mapping OPH dan tanggal RAW.")
    elif raw_oph_audit.get("available") and raw_uncertain_rows:
        proven_pct = 100.0 * raw_proven_rows / gold_rows if gold_rows else 0.0
        print(
            f"WARN baru {raw_proven_rows:,}/{gold_rows:,} usage ({proven_pct:.2f}%) "
            "terbukti exact-day oleh satu tanggal OPH RAW yang unik."
        )
        print(
            f"WARN {raw_uncertain_rows:,} usage belum konklusif; lihat status "
            "RAW_OPH_NOT_FOUND/AMBIGUOUS/DATE_MISSING di atas."
        )
    elif source_mismatch_rows:
        print(
            f"WARN source Context mempunyai {source_mismatch_rows:,} baris beda bulan, "
            "tetapi tidak masuk Gold target."
        )
    elif raw_oph_audit.get("available"):
        print(
            f"OK seluruh {raw_proven_rows:,} usage dibuktikan exact-day oleh "
            "OPH production RAW yang mempunyai satu tanggal bisnis unik."
        )
        print(
            "Jika Mei tetap melonjak, bulan Mei valid secara bisnis; lanjutkan "
            "audit volume source dan pasangan/group, bukan mengubah tanggal."
        )
    else:
        print(
            "OK tidak ditemukan pergeseran bulan antara filename, Context, dan Gold."
        )
        print(
            "Jika Mei tetap melonjak, penyebabnya berada pada volume/duplikasi "
            "gambar Mei atau pembentukan pair, bukan salah penamaan bulan."
        )

    return {
        "canonical_source_group": canonical,
        "run_id": target_run,
        "source_summary": source_summary,
        "source_mismatch": source_mismatch,
        "gold_summary": gold_summary,
        "gold_mismatch_samples": gold_mismatch,
        "source_mismatch_rows": source_mismatch_rows,
        "gold_mismatch_rows": gold_mismatch_rows,
        "gold_rows": gold_rows,
        "raw_oph_audit": raw_oph_audit,
        "raw_proven_rows": raw_proven_rows,
        "raw_mismatch_rows": raw_mismatch_rows,
        "raw_uncertain_rows": raw_uncertain_rows,
    }


def audit_oph_uniqueness(
    settings: Settings,
    source_group: str,
    *,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Prove whether one normalized OPH occurs only once in target Gold.

    The audit keeps three different meanings separate: repeated image usages
    for one OPH inside one group, the same OPH linked to several groups, and
    repeated production rows for one OPH in RAW.  It is read-only.
    """
    canonical, _aliases = _month_audit_aliases(source_group)
    with connection(settings) as conn:
        if run_id:
            target = fetch_all_dict(
                conn,
                """
                SELECT run_id::text AS run_id, source_group
                FROM processing.pipeline_run
                WHERE run_id=%s::uuid
                """,
                (run_id,),
            )
        else:
            target = fetch_all_dict(
                conn,
                """
                SELECT r.run_id::text AS run_id, r.source_group
                FROM processing.pipeline_run r
                WHERE upper(r.source_group)=upper(%s)
                  AND EXISTS (
                      SELECT 1 FROM gold.reused_photo_group g
                      WHERE g.run_id=r.run_id AND g.is_active=true
                  )
                ORDER BY r.started_at DESC
                LIMIT 1
                """,
                (canonical,),
            )
        if not target:
            raise RuntimeError(
                f"Run Gold tidak ditemukan untuk {canonical}"
                + (f": {run_id}" if run_id else "")
            )
        target_run = str(target[0]["run_id"])
        if str(target[0]["source_group"]).upper() != canonical:
            raise RuntimeError(
                f"run_id adalah source_group={target[0]['source_group']}, "
                f"bukan {canonical}"
            )

        scope_cte = """
            WITH gold_scope AS MATERIALIZED (
                SELECT
                    d.group_id,
                    d.file_uid,
                    d.image_name,
                    d.tanggal,
                    d.oph,
                    nullif(processing.normalize_oph(d.oph), '') AS oph_key
                FROM gold.reused_photo_detail d
                WHERE upper(d.source_group)=upper(%s)
                  AND d.run_id=%s::uuid
            )
        """
        params = (canonical, target_run)
        base = fetch_all_dict(
            conn,
            scope_cte
            + """
            SELECT
                count(DISTINCT group_id)::bigint AS groups,
                count(*)::bigint AS usages,
                count(DISTINCT oph_key)
                    FILTER (WHERE oph_key IS NOT NULL) AS distinct_oph,
                count(*) FILTER (WHERE oph_key IS NULL) AS missing_oph
            FROM gold_scope
            """,
            params,
        )[0]

        validated_oph_gate = fetch_all_dict(
            conn,
            """
            SELECT
                coalesce(to_char(c.period_date, 'YYYY-MM'), 'TANPA_TANGGAL')
                    AS month,
                upper(coalesce(c.match_method, 'TANPA_METHOD')) AS match_method,
                count(*)::bigint AS members,
                count(DISTINCT nullif(c.oph_key, ''))::bigint
                    AS distinct_context_oph
            FROM processing.validated_group_member m
            LEFT JOIN processing.image_context c ON c.file_uid=m.file_uid
            WHERE m.run_id=%s::uuid
              AND upper(coalesce(c.match_method, ''))=ANY(%s)
            GROUP BY
                coalesce(to_char(c.period_date, 'YYYY-MM'), 'TANPA_TANGGAL'),
                upper(coalesce(c.match_method, 'TANPA_METHOD'))
            ORDER BY 1, 2
            """,
            (target_run, list(_LEGACY_UNSAFE_OPH_METHODS)),
        )

        within_group = fetch_all_dict(
            conn,
            scope_cte
            + """
            , per_group_oph AS (
                SELECT
                    coalesce(to_char(tanggal, 'YYYY-MM'), 'TANPA_TANGGAL') AS month,
                    group_id,
                    oph_key,
                    count(*)::bigint AS copies
                FROM gold_scope
                WHERE oph_key IS NOT NULL
                GROUP BY
                    coalesce(to_char(tanggal, 'YYYY-MM'), 'TANPA_TANGGAL'),
                    group_id,
                    oph_key
            )
            SELECT
                month,
                count(*) FILTER (WHERE copies>1) AS duplicate_links,
                count(DISTINCT group_id)
                    FILTER (WHERE copies>1) AS affected_groups,
                count(DISTINCT oph_key)
                    FILTER (WHERE copies>1) AS affected_oph,
                coalesce(sum(copies-1) FILTER (WHERE copies>1), 0)::bigint
                    AS extra_usages,
                coalesce(max(copies) FILTER (WHERE copies>1), 0)::bigint
                    AS max_copies_same_group
            FROM per_group_oph
            GROUP BY month
            ORDER BY month
            """,
            params,
        )

        across_groups = fetch_all_dict(
            conn,
            scope_cte
            + """
            , per_group_oph AS (
                SELECT
                    coalesce(to_char(tanggal, 'YYYY-MM'), 'TANPA_TANGGAL') AS month,
                    group_id,
                    oph_key,
                    count(*)::bigint AS copies
                FROM gold_scope
                WHERE oph_key IS NOT NULL
                GROUP BY
                    coalesce(to_char(tanggal, 'YYYY-MM'), 'TANPA_TANGGAL'),
                    group_id,
                    oph_key
            ), per_oph AS (
                SELECT
                    month,
                    oph_key,
                    count(*)::bigint AS group_count,
                    sum(copies)::bigint AS usages
                FROM per_group_oph
                GROUP BY month, oph_key
            )
            SELECT
                month,
                count(*) FILTER (WHERE group_count>1) AS affected_oph,
                coalesce(sum(group_count-1) FILTER (WHERE group_count>1), 0)::bigint
                    AS extra_group_links,
                coalesce(max(group_count) FILTER (WHERE group_count>1), 0)::bigint
                    AS max_groups_per_oph,
                coalesce(max(usages) FILTER (WHERE group_count>1), 0)::bigint
                    AS max_usages_per_oph
            FROM per_oph
            GROUP BY month
            ORDER BY month
            """,
            params,
        )

        top_repeated = fetch_all_dict(
            conn,
            scope_cte
            + """
            , per_group_oph AS (
                SELECT
                    coalesce(to_char(tanggal, 'YYYY-MM'), 'TANPA_TANGGAL') AS month,
                    group_id,
                    oph_key,
                    min(oph) AS oph_sample,
                    count(*)::bigint AS copies
                FROM gold_scope
                WHERE oph_key IS NOT NULL
                GROUP BY
                    coalesce(to_char(tanggal, 'YYYY-MM'), 'TANPA_TANGGAL'),
                    group_id,
                    oph_key
            )
            SELECT
                month,
                oph_key,
                min(oph_sample) AS oph_sample,
                sum(copies)::bigint AS usages,
                count(*)::bigint AS groups,
                sum(copies-1)::bigint AS same_group_extra,
                max(copies)::bigint AS max_copies_same_group
            FROM per_group_oph
            GROUP BY month, oph_key
            HAVING sum(copies)>1
            ORDER BY usages DESC, groups DESC, month, oph_key
            LIMIT 25
            """,
            params,
        )

        cross_month = fetch_all_dict(
            conn,
            scope_cte
            + """
            , per_oph AS (
                SELECT
                    oph_key,
                    count(DISTINCT date_trunc('month', tanggal))::bigint AS months,
                    min(tanggal) AS min_date,
                    max(tanggal) AS max_date,
                    count(*)::bigint AS usages
                FROM gold_scope
                WHERE oph_key IS NOT NULL
                GROUP BY oph_key
            )
            SELECT
                count(*) FILTER (WHERE months>1) AS affected_oph,
                coalesce(max(months) FILTER (WHERE months>1), 0)::bigint
                    AS max_months_per_oph
            FROM per_oph
            """,
            params,
        )[0]

        raw_cardinality: dict[str, Any] = {
            "available": False,
            "warning": None,
            "rows": [],
        }
        raw_schema = settings.raw_business_schema
        raw_table_name = settings.raw_business_table
        raw_exact_evidence: dict[str, Any] = {
            "available": False,
            "warning": None,
            "evidence_columns": [],
            "rows": [],
        }
        raw_actor_capacity: dict[str, Any] = {
            "available": False,
            "warning": None,
            "rows": [],
        }
        raw_timestamp_proof: dict[str, Any] = {
            "available": False,
            "warning": None,
            "rows": [],
        }
        if table_exists(conn, raw_schema, raw_table_name):
            columns = table_columns(conn, raw_schema, raw_table_name)
            raw_oph_col = _find_business_column(columns, BUSINESS_ALIASES["oph"])
            company_col = _find_business_column(
                columns, BUSINESS_ALIASES["company_code"]
            )
            account_status_col = _find_business_column(
                columns, BUSINESS_ALIASES["account_status"]
            )
            if raw_oph_col:
                raw_table = qname(raw_schema, raw_table_name)
                raw_date_sql = _period_date_sql(columns, alias="p")
                join_filters = [
                    f"processing.normalize_oph(p.{ident(raw_oph_col)}::text)=r.oph_key"
                ]
                raw_params: list[object] = []
                if company_col:
                    join_filters.append(
                        f"upper(btrim(p.{ident(company_col)}::text))=upper(%s)"
                    )
                    raw_params.append(settings.source_company(canonical))
                status_values = settings.account_status_values_for(canonical)
                if account_status_col and status_values:
                    join_filters.append(
                        f"upper(btrim(p.{ident(account_status_col)}::text))=ANY(%s)"
                    )
                    raw_params.append(list(status_values))
                raw_rows = fetch_all_dict(
                    conn,
                    scope_cte
                    + f"""
                    , requested_oph AS MATERIALIZED (
                        SELECT DISTINCT oph_key
                        FROM gold_scope
                        WHERE oph_key IS NOT NULL
                    )
                    SELECT
                        r.oph_key,
                        count(p.{ident(raw_oph_col)})::bigint AS raw_rows,
                        count(DISTINCT {raw_date_sql})
                            FILTER (WHERE p.{ident(raw_oph_col)} IS NOT NULL)
                            AS raw_date_count
                    FROM requested_oph r
                    LEFT JOIN {raw_table} p ON {' AND '.join(join_filters)}
                    GROUP BY r.oph_key
                    ORDER BY raw_rows DESC, r.oph_key
                    """,
                    (*params, *raw_params),
                )
                raw_cardinality = {
                    "available": True,
                    "warning": None,
                    "table": f"{raw_schema}.{raw_table_name}",
                    "rows": raw_rows,
                }

                (
                    evidence_columns,
                    evidence_condition,
                    _evidence_priority,
                    _evidence_method,
                ) = _evidence_match_sql(columns, "u.file_name", alias="p")
                resolved, raw_values = _raw_values(columns)
                raw_estate_available = raw_values["estate"] != "NULL::text"
                if evidence_columns and raw_estate_available:
                    exact_filters = [
                        "nullif(processing.normalize_oph("
                        f"{raw_values['oph']}), '') IS NOT NULL",
                        "nullif(btrim(coalesce("
                        f"{raw_values['estate']}, '')), '') IS NOT NULL",
                    ]
                    exact_params: list[object] = [
                        target_run,
                        list(_LEGACY_UNSAFE_OPH_METHODS),
                    ]
                    if company_col:
                        exact_filters.append(
                            "upper(btrim("
                            f"{raw_values['company_code']}))=upper(%s)"
                        )
                        exact_params.append(settings.source_company(canonical))
                    status_values = settings.account_status_values_for(canonical)
                    if account_status_col and status_values:
                        exact_filters.append(
                            f"upper(btrim(p.{ident(account_status_col)}::text))=ANY(%s)"
                        )
                        exact_params.append(list(status_values))

                    exact_rows = fetch_all_dict(
                        conn,
                        f"""
                        WITH unsafe AS MATERIALIZED (
                            SELECT DISTINCT
                                m.file_uid,
                                m.file_name,
                                c.period_date,
                                coalesce(
                                    nullif(c.oph_key, ''),
                                    nullif(processing.normalize_oph(c.oph), '')
                                ) AS context_oph_key,
                                coalesce(
                                    to_char(c.period_date, 'YYYY-MM'),
                                    'TANPA_TANGGAL'
                                ) AS month
                            FROM processing.validated_group_member m
                            LEFT JOIN processing.image_context c
                              ON c.file_uid=m.file_uid
                            WHERE m.run_id=%s::uuid
                              AND upper(coalesce(c.match_method, ''))=ANY(%s)
                        ), raw_matches_all AS MATERIALIZED (
                            SELECT
                                u.file_uid,
                                u.period_date AS context_period_date,
                                processing.normalize_oph({raw_values['oph']})
                                    AS raw_oph_key,
                                upper(btrim({raw_values['estate']})) AS raw_estate,
                                {raw_values['period_date']} AS raw_period_date
                            FROM unsafe u
                            JOIN {raw_table} p ON {evidence_condition}
                            WHERE {' AND '.join(exact_filters)}
                        ), raw_matches AS MATERIALIZED (
                            SELECT *
                            FROM raw_matches_all
                            WHERE raw_period_date=context_period_date
                        ), per_file AS (
                            SELECT
                                u.file_uid,
                                u.month,
                                u.context_oph_key,
                                count(a.raw_oph_key)::bigint AS raw_rows_any_date,
                                count(r.raw_oph_key)::bigint AS raw_rows,
                                count(DISTINCT r.raw_oph_key)::bigint
                                    AS raw_distinct_oph,
                                count(DISTINCT r.raw_estate)::bigint
                                    AS raw_distinct_estate,
                                min(r.raw_oph_key) AS proven_raw_oph_key
                            FROM unsafe u
                            LEFT JOIN raw_matches_all a ON a.file_uid=u.file_uid
                            LEFT JOIN raw_matches r ON r.file_uid=u.file_uid
                            GROUP BY u.file_uid, u.month, u.context_oph_key
                        ), classified AS (
                            SELECT
                                month,
                                CASE
                                    WHEN raw_rows_any_date=0
                                    THEN 'RAW_EVIDENCE_FILENAME_NOT_FOUND'
                                    WHEN raw_rows=0
                                    THEN 'RAW_EVIDENCE_DATE_MISMATCH'
                                    WHEN raw_distinct_oph<>1
                                    THEN 'RAW_EVIDENCE_OPH_CONFLICT'
                                    WHEN raw_distinct_estate<>1
                                    THEN 'RAW_EVIDENCE_ESTATE_CONFLICT'
                                    WHEN context_oph_key=proven_raw_oph_key
                                    THEN 'PROVEN_CONTEXT_OPH_EXACT'
                                    ELSE 'PROVEN_RAW_OPH_REPLACEMENT'
                                END AS status
                            FROM per_file
                        )
                        SELECT month, status, count(*)::bigint AS members
                        FROM classified
                        GROUP BY month, status
                        ORDER BY month, status
                        """,
                        exact_params,
                    )
                    raw_exact_evidence = {
                        "available": True,
                        "warning": None,
                        "evidence_columns": evidence_columns,
                        "rows": exact_rows,
                    }
                elif not evidence_columns:
                    raw_exact_evidence["warning"] = (
                        "RAW tidak mempunyai kolom evidence filename"
                    )
                else:
                    raw_exact_evidence["warning"] = (
                        "RAW tidak mempunyai kolom estate"
                    )

                # Filename Harmony lama tidak selalu menyimpan basename
                # FFB_EVIDENCE_KP. Untuk kasus tersebut audit kapasitas
                # rekonstruksi satu-ke-satu berdasarkan actor+tanggal+estate,
                # sama dengan recovery aman yang tidak memakai OPH arbitrer.
                column_map = {column.lower(): column for column in columns}
                actor_columns = list(
                    dict.fromkeys(
                        column
                        for column in (
                            column_map.get("generated_by"),
                            resolved.get("nik_pemanen"),
                            resolved.get("nik_kerani"),
                            resolved.get("nik_mandor"),
                        )
                        if column
                    )
                )
                raw_period_available = raw_values["period_date"] != "NULL::date"
                if actor_columns and raw_estate_available and raw_period_available:
                    raw_actor_array = (
                        "array_remove(ARRAY["
                        + ", ".join(
                            "NULLIF(regexp_replace(regexp_replace(upper(btrim("
                            f"p.{ident(column)}::text)), '[^A-Z0-9]', '', 'g'), "
                            "'^0+', ''), '')"
                            for column in actor_columns
                        )
                        + "]::text[], NULL)"
                    )
                    actor_source_pk = (
                        raw_values["source_pk"]
                        if resolved.get("source_pk")
                        else "NULL::text"
                    )
                    actor_account_priority = "2"
                    if account_status_col:
                        actor_account_priority = (
                            f"CASE upper(btrim(p.{ident(account_status_col)}::text)) "
                            "WHEN 'ACTUAL APPROVE' THEN 0 "
                            "WHEN 'ACTUAL' THEN 1 ELSE 2 END"
                        )
                    filename_actor = """
                        NULLIF(
                            regexp_replace(
                                regexp_replace(
                                    upper(
                                        CASE
                                            WHEN split_part(m.file_name, '_', 1)
                                                 ~ '^[0-9]{14}$'
                                            THEN split_part(m.file_name, '_', 2)
                                            ELSE split_part(m.file_name, '_', 1)
                                        END
                                    ),
                                    '[^A-Z0-9]', '', 'g'
                                ),
                                '^0+', ''
                            ),
                            ''
                        )
                    """
                    generated_at_col = column_map.get("generated_at")
                    filename_timestamp = """
                        CASE
                            WHEN split_part(m.file_name, '_', 1) ~ '^[0-9]{14}$'
                            THEN split_part(m.file_name, '_', 1)
                            WHEN split_part(m.file_name, '_', 2) ~ '^20[0-9]{6}$'
                             AND split_part(m.file_name, '_', 3) ~ '^[0-9]{6}'
                            THEN split_part(m.file_name, '_', 2)
                                 || left(split_part(m.file_name, '_', 3), 6)
                            ELSE NULL::text
                        END
                    """
                    actor_filters = [
                        "nullif(processing.normalize_oph("
                        f"{raw_values['oph']}), '') IS NOT NULL",
                        "nullif(btrim(coalesce("
                        f"{raw_values['estate']}, '')), '') IS NOT NULL",
                    ]
                    actor_params: list[object] = [
                        target_run,
                        list(_LEGACY_UNSAFE_OPH_METHODS),
                        target_run,
                        list(_LEGACY_UNSAFE_OPH_METHODS),
                    ]
                    if company_col:
                        actor_filters.append(
                            "upper(btrim("
                            f"{raw_values['company_code']}))=upper(%s)"
                        )
                        actor_params.append(settings.source_company(canonical))
                    if account_status_col and status_values:
                        actor_filters.append(
                            f"upper(btrim(p.{ident(account_status_col)}::text))=ANY(%s)"
                        )
                        actor_params.append(list(status_values))

                    if generated_at_col:
                        timestamp_params: list[object] = [
                            target_run,
                            list(_LEGACY_UNSAFE_OPH_METHODS),
                        ]
                        if company_col:
                            timestamp_params.append(
                                settings.source_company(canonical)
                            )
                        if account_status_col and status_values:
                            timestamp_params.append(list(status_values))
                        timestamp_rows = fetch_all_dict(
                            conn,
                            f"""
                            WITH unsafe AS MATERIALIZED (
                                SELECT DISTINCT
                                    m.file_uid,
                                    coalesce(
                                        to_char(c.period_date, 'YYYY-MM'),
                                        'TANPA_TANGGAL'
                                    ) AS month,
                                    {filename_actor} AS actor_key,
                                    {filename_timestamp} AS timestamp_key,
                                    c.period_date,
                                    upper(btrim(c.estate)) AS estate_key,
                                    coalesce(
                                        nullif(c.oph_key, ''),
                                        nullif(processing.normalize_oph(c.oph), '')
                                    ) AS context_oph_key
                                FROM processing.validated_group_member m
                                JOIN processing.image_context c
                                  ON c.file_uid=m.file_uid
                                WHERE m.run_id=%s::uuid
                                  AND upper(coalesce(c.match_method, ''))=ANY(%s)
                            ), requested AS MATERIALIZED (
                                SELECT DISTINCT
                                    actor_key, timestamp_key,
                                    period_date, estate_key
                                FROM unsafe
                                WHERE actor_key IS NOT NULL
                                  AND timestamp_key IS NOT NULL
                                  AND period_date IS NOT NULL
                                  AND nullif(estate_key, '') IS NOT NULL
                            ), raw_matches AS MATERIALIZED (
                                SELECT
                                    q.actor_key,
                                    q.timestamp_key,
                                    q.period_date,
                                    q.estate_key,
                                    processing.normalize_oph({raw_values['oph']})
                                        AS raw_oph_key
                                FROM {raw_table} p
                                CROSS JOIN LATERAL unnest({raw_actor_array})
                                    AS actor(actor_key)
                                JOIN requested q
                                  ON q.actor_key=actor.actor_key
                                 AND q.timestamp_key=to_char(
                                     p.{ident(generated_at_col)}::timestamp,
                                     'YYYYMMDDHH24MISS'
                                 )
                                 AND q.period_date={raw_values['period_date']}
                                 AND q.estate_key=upper(btrim({raw_values['estate']}))
                                WHERE {' AND '.join(actor_filters)}
                            ), per_file AS (
                                SELECT
                                    u.file_uid,
                                    u.month,
                                    u.context_oph_key,
                                    u.actor_key,
                                    u.timestamp_key,
                                    count(r.raw_oph_key)::bigint AS raw_rows,
                                    count(DISTINCT r.raw_oph_key)::bigint
                                        AS raw_distinct_oph,
                                    min(r.raw_oph_key) AS proven_raw_oph_key
                                FROM unsafe u
                                LEFT JOIN raw_matches r
                                  ON r.actor_key=u.actor_key
                                 AND r.timestamp_key=u.timestamp_key
                                 AND r.period_date=u.period_date
                                 AND r.estate_key=u.estate_key
                                GROUP BY
                                    u.file_uid, u.month, u.context_oph_key,
                                    u.actor_key, u.timestamp_key
                            ), classified AS (
                                SELECT
                                    month,
                                    CASE
                                        WHEN timestamp_key IS NULL
                                        THEN 'FILENAME_TIMESTAMP_MISSING'
                                        WHEN actor_key IS NULL
                                        THEN 'FILENAME_ACTOR_MISSING'
                                        WHEN raw_rows=0
                                        THEN 'RAW_TIMESTAMP_ACTOR_NOT_FOUND'
                                        WHEN raw_distinct_oph<>1
                                        THEN 'RAW_TIMESTAMP_ACTOR_OPH_CONFLICT'
                                        WHEN context_oph_key=proven_raw_oph_key
                                        THEN 'PROVEN_CONTEXT_OPH_TIMESTAMP'
                                        ELSE 'PROVEN_RAW_OPH_TIMESTAMP_REPLACEMENT'
                                    END AS status
                                FROM per_file
                            )
                            SELECT month, status, count(*)::bigint AS members
                            FROM classified
                            GROUP BY month, status
                            ORDER BY month, status
                            """,
                            timestamp_params,
                        )
                        raw_timestamp_proof = {
                            "available": True,
                            "warning": None,
                            "generated_at_column": generated_at_col,
                            "rows": timestamp_rows,
                        }
                    else:
                        raw_timestamp_proof["warning"] = (
                            "RAW tidak mempunyai kolom generated_at"
                        )

                    actor_rows = fetch_all_dict(
                        conn,
                        f"""
                        WITH unsafe AS MATERIALIZED (
                            SELECT DISTINCT
                                m.file_uid,
                                coalesce(
                                    to_char(c.period_date, 'YYYY-MM'),
                                    'TANPA_TANGGAL'
                                ) AS month,
                                {filename_actor} AS actor_key,
                                c.period_date,
                                upper(btrim(c.estate)) AS estate_key
                            FROM processing.validated_group_member m
                            JOIN processing.image_context c
                              ON c.file_uid=m.file_uid
                            WHERE m.run_id=%s::uuid
                              AND upper(coalesce(c.match_method, ''))=ANY(%s)
                        ), claimed_oph AS MATERIALIZED (
                            SELECT DISTINCT
                                processing.normalize_oph(c.oph) AS oph_key
                            FROM processing.validated_group_member m
                            JOIN processing.image_context c
                              ON c.file_uid=m.file_uid
                            WHERE m.run_id=%s::uuid
                              AND upper(coalesce(c.match_method, ''))<>ALL(%s)
                              AND nullif(processing.normalize_oph(c.oph), '')
                                  IS NOT NULL
                        ), requested AS MATERIALIZED (
                            SELECT DISTINCT actor_key, period_date, estate_key
                            FROM unsafe
                            WHERE actor_key IS NOT NULL
                              AND period_date IS NOT NULL
                              AND nullif(estate_key, '') IS NOT NULL
                        ), raw_candidates AS MATERIALIZED (
                            SELECT
                                actor.actor_key,
                                {raw_values['period_date']} AS period_date,
                                upper(btrim({raw_values['estate']})) AS estate_key,
                                processing.normalize_oph({raw_values['oph']})
                                    AS oph_key,
                                actor.actor_priority,
                                {actor_account_priority} AS account_priority,
                                {actor_source_pk} AS source_pk
                            FROM {raw_table} p
                            CROSS JOIN LATERAL unnest({raw_actor_array})
                                WITH ORDINALITY AS actor(actor_key, actor_priority)
                            JOIN requested q
                              ON q.actor_key=actor.actor_key
                             AND q.period_date={raw_values['period_date']}
                             AND q.estate_key=upper(btrim({raw_values['estate']}))
                            WHERE {' AND '.join(actor_filters)}
                        ), raw_available AS MATERIALIZED (
                            SELECT DISTINCT ON (r.oph_key)
                                r.actor_key, r.period_date, r.estate_key, r.oph_key
                            FROM raw_candidates r
                            LEFT JOIN claimed_oph c ON c.oph_key=r.oph_key
                            WHERE c.oph_key IS NULL
                            ORDER BY r.oph_key, r.actor_priority,
                                     r.account_priority,
                                     r.source_pk DESC NULLS LAST
                        ), usage_bucket AS (
                            SELECT
                                month, actor_key, period_date, estate_key,
                                count(*)::bigint AS unsafe_members
                            FROM unsafe
                            WHERE actor_key IS NOT NULL
                              AND period_date IS NOT NULL
                              AND nullif(estate_key, '') IS NOT NULL
                            GROUP BY month, actor_key, period_date, estate_key
                        ), raw_bucket AS (
                            SELECT
                                actor_key, period_date, estate_key,
                                count(*)::bigint AS available_oph
                            FROM raw_available
                            GROUP BY actor_key, period_date, estate_key
                        ), capacity AS (
                            SELECT
                                u.month,
                                u.unsafe_members,
                                coalesce(r.available_oph, 0)::bigint AS available_oph
                            FROM usage_bucket u
                            LEFT JOIN raw_bucket r
                              ON r.actor_key=u.actor_key
                             AND r.period_date=u.period_date
                             AND r.estate_key=u.estate_key
                        ), actor_missing AS (
                            SELECT month, count(*)::bigint AS members
                            FROM unsafe
                            WHERE actor_key IS NULL
                               OR period_date IS NULL
                               OR nullif(estate_key, '') IS NULL
                            GROUP BY month
                        ), months AS (
                            SELECT DISTINCT month FROM unsafe
                        )
                        SELECT
                            months.month,
                            coalesce(sum(capacity.unsafe_members), 0)::bigint
                                + coalesce(max(actor_missing.members), 0)::bigint
                                AS unsafe_members,
                            coalesce(max(actor_missing.members), 0)::bigint
                                AS actor_key_missing,
                            coalesce(sum(capacity.available_oph), 0)::bigint
                                AS available_raw_oph,
                            coalesce(sum(least(
                                capacity.unsafe_members,
                                capacity.available_oph
                            )), 0)::bigint AS projected_recoverable,
                            coalesce(sum(greatest(
                                capacity.unsafe_members-capacity.available_oph,
                                0
                            )), 0)::bigint
                                + coalesce(max(actor_missing.members), 0)::bigint
                                AS projected_unresolved,
                            count(*) FILTER (
                                WHERE capacity.available_oph
                                      < capacity.unsafe_members
                            )::bigint AS shortage_buckets
                        FROM months
                        LEFT JOIN capacity ON capacity.month=months.month
                        LEFT JOIN actor_missing
                          ON actor_missing.month=months.month
                        GROUP BY months.month
                        ORDER BY months.month
                        """,
                        actor_params,
                    )
                    raw_actor_capacity = {
                        "available": True,
                        "warning": None,
                        "actor_columns": actor_columns,
                        "rows": actor_rows,
                    }
                else:
                    raw_actor_capacity["warning"] = (
                        "kolom actor/date/estate RAW tidak lengkap"
                    )
                    raw_timestamp_proof["warning"] = (
                        "kolom actor/date/estate RAW tidak lengkap"
                    )
            else:
                raw_cardinality["warning"] = "kolom OPH RAW tidak ditemukan"
                raw_exact_evidence["warning"] = "kolom OPH RAW tidak ditemukan"
                raw_actor_capacity["warning"] = "kolom OPH RAW tidak ditemukan"
                raw_timestamp_proof["warning"] = "kolom OPH RAW tidak ditemukan"
        else:
            raw_cardinality["warning"] = (
                f"tabel RAW tidak ditemukan: {raw_schema}.{raw_table_name}"
            )
            raw_exact_evidence["warning"] = raw_cardinality["warning"]
            raw_actor_capacity["warning"] = raw_cardinality["warning"]
            raw_timestamp_proof["warning"] = raw_cardinality["warning"]

    usages = int(base.get("usages") or 0)
    distinct_oph = int(base.get("distinct_oph") or 0)
    missing_oph = int(base.get("missing_oph") or 0)
    duplicate_occurrences = max(0, usages - missing_oph - distinct_oph)

    print("=" * 78)
    print(f"OPH UNIQUENESS AUDIT: {canonical} | run_id={target_run}")
    print("=" * 78)
    print(
        f"GOLD groups={int(base.get('groups') or 0):,} usages={usages:,} "
        f"distinct_oph={distinct_oph:,} missing_oph={missing_oph:,}"
    )
    print(
        f"ONE-OPH-ONE-USAGE duplicate_occurrences={duplicate_occurrences:,}"
    )

    print("-" * 78)
    print("VALIDATED OPH PUBLISH GATE (termasuk member yang tidak masuk Gold)")
    unsafe_total = 0
    if validated_oph_gate:
        print(
            f"{'MONTH':<13} {'MATCH METHOD':<43} {'MEMBERS':>9} "
            f"{'DIST.OPH':>10}"
        )
        for row in validated_oph_gate:
            members = int(row.get("members") or 0)
            unsafe_total += members
            print(
                f"{str(row['month']):<13} "
                f"{str(row['match_method']):<43} "
                f"{members:>9,} "
                f"{int(row.get('distinct_context_oph') or 0):>10,}"
            )
    else:
        print("OK tidak ada member validated dengan provenance OPH legacy unsafe.")

    print("-" * 78)
    print("RAW/DB45 EXACT FILENAME EVIDENCE PROOF (read-only)")
    raw_exact_proven = 0
    raw_exact_replacement = 0
    raw_exact_unresolved = 0
    if raw_exact_evidence.get("available"):
        print(
            "EVIDENCE_COLUMNS="
            + ",".join(raw_exact_evidence.get("evidence_columns") or [])
        )
        print(f"{'MONTH':<13} {'STATUS':<42} {'MEMBERS':>9}")
        for row in raw_exact_evidence.get("rows") or []:
            status = str(row.get("status") or "")
            members = int(row.get("members") or 0)
            if status == "PROVEN_CONTEXT_OPH_EXACT":
                raw_exact_proven += members
            elif status == "PROVEN_RAW_OPH_REPLACEMENT":
                raw_exact_replacement += members
            else:
                raw_exact_unresolved += members
            print(
                f"{str(row['month']):<13} {status:<42} {members:>9,}"
            )
        print(
            "SUMMARY "
            f"unsafe={unsafe_total:,} "
            f"context_oph_proven={raw_exact_proven:,} "
            f"raw_oph_replacement={raw_exact_replacement:,} "
            f"unresolved={raw_exact_unresolved:,}"
        )
    else:
        print(f"WARN {raw_exact_evidence.get('warning')}")

    print("-" * 78)
    print("RAW/DB45 EXACT TIMESTAMP+ACTOR PROOF (read-only)")
    if raw_timestamp_proof.get("available"):
        print(
            "GENERATED_AT_COLUMN="
            + str(raw_timestamp_proof.get("generated_at_column") or "-")
        )
        print(f"{'MONTH':<13} {'STATUS':<43} {'MEMBERS':>9}")
        for row in raw_timestamp_proof.get("rows") or []:
            print(
                f"{str(row['month']):<13} "
                f"{str(row.get('status') or ''):<43} "
                f"{int(row.get('members') or 0):>9,}"
            )
    else:
        print(f"WARN {raw_timestamp_proof.get('warning')}")

    print("-" * 78)
    print("RAW/DB45 ACTOR+DATE+ESTATE CAPACITY ONLY (read-only)")
    if raw_actor_capacity.get("available"):
        print(
            "ACTOR_COLUMNS="
            + ",".join(raw_actor_capacity.get("actor_columns") or [])
        )
        print(
            f"{'MONTH':<10} {'UNSAFE':>9} {'ACTOR?':>9} {'RAW OPH':>10} "
            f"{'CAPACITY':>10} {'NOT COVERED':>12} {'SHORT BUCKET':>13}"
        )
        for row in raw_actor_capacity.get("rows") or []:
            print(
                f"{str(row['month']):<10} "
                f"{int(row.get('unsafe_members') or 0):>9,} "
                f"{int(row.get('actor_key_missing') or 0):>9,} "
                f"{int(row.get('available_raw_oph') or 0):>10,} "
                f"{int(row.get('projected_recoverable') or 0):>10,} "
                f"{int(row.get('projected_unresolved') or 0):>12,} "
                f"{int(row.get('shortage_buckets') or 0):>13,}"
            )
        print(
            "WARN CAPACITY bukan bukti OPH milik foto. Jangan publish hanya "
            "karena kapasitas cukup; gunakan exact filename/timestamp proof."
        )
    else:
        print(f"WARN {raw_actor_capacity.get('warning')}")

    print("-" * 78)
    print("OPH BERULANG DI DALAM GROUP YANG SAMA")
    print(
        f"{'MONTH':<10} {'DUP.LINK':>10} {'GROUPS':>9} {'OPH':>9} "
        f"{'EXTRA':>9} {'MAX COPY':>10}"
    )
    for row in within_group:
        print(
            f"{str(row['month']):<10} "
            f"{int(row['duplicate_links'] or 0):>10,} "
            f"{int(row['affected_groups'] or 0):>9,} "
            f"{int(row['affected_oph'] or 0):>9,} "
            f"{int(row['extra_usages'] or 0):>9,} "
            f"{int(row['max_copies_same_group'] or 0):>10,}"
        )

    print("-" * 78)
    print("OPH YANG SAMA MASUK KE BEBERAPA GROUP")
    print(
        f"{'MONTH':<10} {'OPH':>9} {'EXTRA LINK':>12} "
        f"{'MAX GROUP':>11} {'MAX USAGE':>11}"
    )
    for row in across_groups:
        print(
            f"{str(row['month']):<10} "
            f"{int(row['affected_oph'] or 0):>9,} "
            f"{int(row['extra_group_links'] or 0):>12,} "
            f"{int(row['max_groups_per_oph'] or 0):>11,} "
            f"{int(row['max_usages_per_oph'] or 0):>11,}"
        )

    print("-" * 78)
    print("TOP OPH BERULANG (maksimum 25)")
    for row in top_repeated:
        print(
            f"  {row['month']} oph={row['oph_sample'] or row['oph_key']} "
            f"usages={int(row['usages'] or 0):,} "
            f"groups={int(row['groups'] or 0):,} "
            f"same_group_extra={int(row['same_group_extra'] or 0):,} "
            f"max_copy={int(row['max_copies_same_group'] or 0):,}"
        )

    print("-" * 78)
    print(
        f"CROSS-MONTH affected_oph={int(cross_month.get('affected_oph') or 0):,} "
        f"max_months_per_oph={int(cross_month.get('max_months_per_oph') or 0):,}"
    )
    print("RAW OPH ROW CARDINALITY (baris RAW berulang belum tentu transaksi ganda)")
    raw_duplicate_oph = 0
    raw_extra_rows = 0
    if raw_cardinality.get("available"):
        raw_rows = list(raw_cardinality.get("rows") or [])
        raw_duplicate_oph = sum(1 for row in raw_rows if int(row["raw_rows"] or 0) > 1)
        raw_extra_rows = sum(
            max(0, int(row["raw_rows"] or 0) - 1) for row in raw_rows
        )
        print(
            f"RAW={raw_cardinality['table']} requested_oph={len(raw_rows):,} "
            f"oph_multirow={raw_duplicate_oph:,} extra_raw_rows={raw_extra_rows:,}"
        )
        for row in raw_rows[:15]:
            if int(row["raw_rows"] or 0) <= 1:
                break
            print(
                f"  oph_key={row['oph_key']} raw_rows={int(row['raw_rows']):,} "
                f"distinct_dates={int(row['raw_date_count'] or 0):,}"
            )
    else:
        print(f"WARN {raw_cardinality.get('warning')}")

    print("-" * 78)
    if duplicate_occurrences:
        print(
            f"FAIL one-OPH-one-usage belum terpenuhi: ada "
            f"{duplicate_occurrences:,} occurrence OPH tambahan di Gold."
        )
        print(
            "ACTION jangan menghapus otomatis dahulu; gunakan pembagian within-group "
            "dan across-group di atas untuk menentukan aturan pemenang OPH."
        )
    elif missing_oph:
        print(
            f"INFO ada {missing_oph:,} visual usage tanpa OPH terverifikasi; "
            "foto tetap tampil tetapi tidak ikut audit/KPI transaksi OPH."
        )
    else:
        print("OK setiap OPH target hanya muncul satu kali di Gold.")

    return {
        "canonical_source_group": canonical,
        "run_id": target_run,
        "base": base,
        "validated_oph_gate": validated_oph_gate,
        "raw_exact_evidence": raw_exact_evidence,
        "raw_timestamp_proof": raw_timestamp_proof,
        "raw_actor_capacity": raw_actor_capacity,
        "duplicate_occurrences": duplicate_occurrences,
        "within_group": within_group,
        "across_groups": across_groups,
        "top_repeated": top_repeated,
        "cross_month": cross_month,
        "raw_cardinality": raw_cardinality,
        "raw_duplicate_oph": raw_duplicate_oph,
        "raw_extra_rows": raw_extra_rows,
    }
