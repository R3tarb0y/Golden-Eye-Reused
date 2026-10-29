from __future__ import annotations

from typing import Any

from .config import Settings
from .db import connection, qname, table_exists


_SOURCE_MAP = {
    "DAN": "ELHP_DAN",
    "DWT": "ELHP_DWT",
}


def _month_rows(conn, settings: Settings, sources: list[str]) -> list[dict[str, Any]]:
    image_table = qname(settings.silver_schema, settings.image_table)
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT upper(source_group::text) AS source_group,
                   CASE
                       WHEN (regexp_match(file_name::text, '(20[0-9]{{6}})'))[1]
                            ~ '^20[0-9]{{2}}(0[1-9]|1[0-2])[0-3][0-9]$'
                       THEN left((regexp_match(file_name::text, '(20[0-9]{{6}})'))[1], 4)
                            || '-'
                            || substring((regexp_match(file_name::text, '(20[0-9]{{6}})'))[1], 5, 2)
                       WHEN (regexp_match(file_name::text, '(20[0-9]{{4}})'))[1]
                            ~ '^20[0-9]{{2}}(0[1-9]|1[0-2])$'
                       THEN left((regexp_match(file_name::text, '(20[0-9]{{4}})'))[1], 4)
                            || '-'
                            || right((regexp_match(file_name::text, '(20[0-9]{{4}})'))[1], 2)
                       ELSE 'TANPA_BULAN'
                   END AS month,
                   count(*)::bigint AS rows,
                   count(DISTINCT upper(trim(file_name::text)))::bigint AS logical_files
            FROM {image_table}
            WHERE upper(source_group::text)=ANY(%s)
              AND coalesce(is_transport, false)=false
            GROUP BY 1, 2
            ORDER BY 1, 2
            """,
            (sources,),
        )
        columns = [item.name if hasattr(item, "name") else item[0] for item in cur.description]
        return [dict(zip(columns, row)) for row in cur.fetchall()]


def consolidate_elhp_source(
    settings: Settings,
    source_group: str,
    *,
    apply: bool = False,
) -> dict[str, Any]:
    canonical = str(source_group or "").upper().strip()
    if canonical not in _SOURCE_MAP:
        raise ValueError("consolidate-elhp hanya menerima DAN atau DWT")
    alias = _SOURCE_MAP[canonical]
    sources = [canonical, alias]

    with connection(settings) as conn:
        before = _month_rows(conn, settings, sources)
        print("=" * 78)
        print(f"ELHP SOURCE CONSOLIDATION: {alias} -> {canonical}")
        print("=" * 78)
        for row in before:
            print(
                f"{row['source_group']:<12} {row['month']:<14} "
                f"rows={int(row['rows']):>10,} logical={int(row['logical_files']):>10,}"
            )
        alias_rows = sum(
            int(row["rows"]) for row in before if row["source_group"] == alias
        )
        if not apply:
            print(f"DRY-RUN alias_rows={alias_rows:,}; ulangi dengan --apply")
            return {
                "canonical": canonical,
                "alias": alias,
                "alias_rows": alias_rows,
                "applied": False,
                "months": before,
            }

        image_table = qname(settings.silver_schema, settings.image_table)
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE {image_table} SET source_group=%s "
                "WHERE upper(source_group::text)=%s",
                (canonical, alias),
            )
            image_updated = max(0, cur.rowcount)

            for table in ("image_feature", "image_context"):
                if table_exists(conn, "processing", table):
                    cur.execute(
                        f'UPDATE processing."{table}" SET source_group=%s '
                        "WHERE upper(source_group)=%s",
                        (canonical, alias),
                    )

            # Recovery BYTEA besar tidak disalin. Pindahkan key hanya bila belum
            # ada canonical twin; konflik lama cukup dinonaktifkan.
            for table in ("gambar_reused", "source_bytea_exception"):
                if not table_exists(conn, "processing", table):
                    continue
                cur.execute(
                    f"""
                    UPDATE processing."{table}" a
                    SET source_group=%s
                    WHERE upper(a.source_group)=%s
                      AND NOT EXISTS (
                          SELECT 1 FROM processing."{table}" c
                          WHERE upper(c.source_group)=%s
                            AND c.file_uid=a.file_uid
                      )
                    """,
                    (canonical, alias, canonical),
                )
                cur.execute(
                    f'UPDATE processing."{table}" SET is_active=false '
                    "WHERE upper(source_group)=%s",
                    (alias,),
                )

            for table in (
                "reused_photo_group",
                "reused_photo_detail",
                "reused_photo_group_summary",
                "reused_photo_dashboard_monthly",
                "oph_audit",
                "sap_oph_kpi_summary",
            ):
                if table_exists(conn, "gold", table):
                    cur.execute(
                        f'UPDATE gold."{table}" SET is_active=false '
                        "WHERE upper(source_group)=%s AND is_active=true",
                        (alias,),
                    )

        after = _month_rows(conn, settings, sources)

    print(f"APPLIED image_rows={image_updated:,}; alias Gold dinonaktifkan")
    print(
        f"NEXT py run_pipeline.py run --source-group {canonical} "
        "--from-stage context --to-stage publish"
    )
    return {
        "canonical": canonical,
        "alias": alias,
        "alias_rows": alias_rows,
        "image_rows_updated": image_updated,
        "applied": True,
        "months": after,
    }
