from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime
from pathlib import Path

from src.golden_eye.config import Settings
from src.golden_eye.db import connection


def human_bytes(value):
    if value is None:
        return "-"
    value = float(value)
    units = ["B", "KB", "MB", "GB", "TB"]
    for unit in units:
        if abs(value) < 1024.0 or unit == units[-1]:
            return f"{value:,.2f} {unit}"
        value /= 1024.0
    return f"{value:,.2f} TB"


def fetch_all(cur, sql, params=None):
    cur.execute(sql, params or ())
    cols = [d.name if hasattr(d, "name") else d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def fetch_one(cur, sql, params=None):
    rows = fetch_all(cur, sql, params)
    return rows[0] if rows else {}


def regclass_exists(cur, qname):
    cur.execute("SELECT to_regclass(%s)", (qname,))
    return cur.fetchone()[0] is not None


def column_exists(cur, schema, table, column):
    cur.execute(
        """
        SELECT EXISTS (
          SELECT 1 FROM information_schema.columns
          WHERE table_schema=%s AND table_name=%s AND column_name=%s
        )
        """,
        (schema, table, column),
    )
    return bool(cur.fetchone()[0])


def qident(v):
    return '"' + str(v).replace('"', '""') + '"'


def qname(schema, table):
    return f"{qident(schema)}.{qident(table)}"


def main():
    parser = argparse.ArgumentParser(
        description="READ-ONLY storage audit Golden Eye PostgreSQL. Tidak DELETE/UPDATE/VACUUM."
    )
    parser.add_argument("--top", type=int, default=40, help="Jumlah relation terbesar")
    parser.add_argument(
        "--deep",
        action="store_true",
        help="Tambahkan exact counts/byte totals. Lebih lambat tetapi tetap read-only.",
    )
    parser.add_argument("--output-dir", default="storage_audit")
    args = parser.parse_args()

    settings = Settings()
    outdir = Path(args.output_dir).resolve()
    outdir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    txt_path = outdir / f"golden_eye_pg_storage_{stamp}.txt"
    csv_path = outdir / f"golden_eye_pg_relations_{stamp}.csv"
    json_path = outdir / f"golden_eye_pg_storage_{stamp}.json"

    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "database": settings.pg_database,
        "deep": bool(args.deep),
        "database_summary": {},
        "relations": [],
        "payloads": {},
        "pipeline_runs": {},
        "notes": [],
    }

    with connection(settings) as conn:
        conn.set_session(readonly=True, autocommit=False)
        with conn.cursor() as cur:
            cur.execute("SET LOCAL statement_timeout = '15min'")
            db = fetch_one(
                cur,
                """
                SELECT current_database() AS database_name,
                       pg_database_size(current_database())::bigint AS database_bytes,
                       pg_size_pretty(pg_database_size(current_database())) AS database_size,
                       current_setting('server_version') AS server_version
                """,
            )
            report["database_summary"] = db

            rels = fetch_all(
                cur,
                """
                SELECT
                    n.nspname AS schema_name,
                    c.relname AS relation_name,
                    CASE c.relkind WHEN 'r' THEN 'table' WHEN 'm' THEN 'materialized_view' ELSE c.relkind::text END AS relation_type,
                    COALESCE(s.n_live_tup, c.reltuples)::bigint AS estimated_rows,
                    pg_total_relation_size(c.oid)::bigint AS total_bytes,
                    pg_relation_size(c.oid)::bigint AS heap_bytes,
                    pg_indexes_size(c.oid)::bigint AS index_bytes,
                    CASE WHEN c.reltoastrelid <> 0 THEN pg_total_relation_size(c.reltoastrelid)::bigint ELSE 0 END AS toast_bytes
                FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                LEFT JOIN pg_stat_user_tables s ON s.relid = c.oid
                WHERE c.relkind IN ('r','m')
                  AND n.nspname NOT IN ('pg_catalog','information_schema')
                  AND n.nspname NOT LIKE 'pg_toast%'
                ORDER BY pg_total_relation_size(c.oid) DESC
                LIMIT %s
                """,
                (max(1, args.top),),
            )
            report["relations"] = rels

            # Discover BYTEA columns without touching their payload.
            bytea_cols = fetch_all(
                cur,
                """
                SELECT table_schema, table_name, column_name
                FROM information_schema.columns
                WHERE data_type='bytea'
                  AND table_schema NOT IN ('pg_catalog','information_schema')
                ORDER BY table_schema, table_name, ordinal_position
                """,
            )
            report["bytea_columns"] = bytea_cols

            # Silver raw image payload: uses bytea_len, so totals do not need to detoast image_bytes.
            silver_schema = settings.silver_schema
            silver_table = settings.image_table
            silver_q = f"{silver_schema}.{silver_table}"
            if regclass_exists(cur, silver_q):
                cols_ok = all(
                    column_exists(cur, silver_schema, silver_table, c)
                    for c in ("image_bytes", "bytea_len")
                )
                if cols_ok:
                    silver = fetch_one(
                        cur,
                        f"""
                        SELECT
                          count(*)::bigint AS rows,
                          count(*) FILTER (WHERE image_bytes IS NOT NULL)::bigint AS image_bytea_rows,
                          coalesce(sum(bytea_len) FILTER (WHERE image_bytes IS NOT NULL),0)::bigint AS image_payload_bytes,
                          count(*) FILTER (WHERE image_bytes IS NULL)::bigint AS metadata_only_rows
                        FROM {qname(silver_schema, silver_table)}
                        """,
                    )
                    report["payloads"][silver_q] = silver

            # Hot evidence cache payload.
            if regclass_exists(cur, "processing.gambar_reused"):
                hot = fetch_one(
                    cur,
                    """
                    SELECT count(*)::bigint AS rows,
                           coalesce(sum(bytea_len),0)::bigint AS image_payload_bytes,
                           count(*) FILTER (WHERE is_active)::bigint AS active_rows
                    FROM processing.gambar_reused
                    """,
                )
                report["payloads"]["processing.gambar_reused"] = hot

            # Embeddings are float16 (2 bytes/dimension) in Golden Eye. Estimate exact logical payload
            # from embedding_dim without forcing PostgreSQL to read/detoast every vector.
            if regclass_exists(cur, "processing.image_feature"):
                emb = fetch_one(
                    cur,
                    """
                    SELECT count(*)::bigint AS rows,
                           count(*) FILTER (WHERE full_embedding IS NOT NULL)::bigint AS full_rows,
                           count(*) FILTER (WHERE center_embedding IS NOT NULL)::bigint AS center_rows,
                           coalesce(sum(CASE WHEN full_embedding IS NOT NULL THEN embedding_dim::bigint * 2 ELSE 0 END),0)::bigint AS full_embedding_est_bytes,
                           coalesce(sum(CASE WHEN center_embedding IS NOT NULL THEN embedding_dim::bigint * 2 ELSE 0 END),0)::bigint AS center_embedding_est_bytes,
                           min(embedding_dim) FILTER (WHERE embedding_dim > 0) AS min_dim,
                           max(embedding_dim) FILTER (WHERE embedding_dim > 0) AS max_dim
                    FROM processing.image_feature
                    """,
                )
                emb["logical_embedding_est_bytes"] = int(emb.get("full_embedding_est_bytes") or 0) + int(emb.get("center_embedding_est_bytes") or 0)
                report["payloads"]["processing.image_feature"] = emb

            # Intermediates and final structures: rely on relation sizes, counts optional.
            tracked = [
                "processing.candidate_edge",
                "processing.candidate_group",
                "processing.candidate_group_member",
                "processing.validation_pair_audit",
                "processing.validation_progress",
                "processing.validated_group",
                "processing.validated_group_member",
                "processing.transport_kt_similarity",
            ]
            for rel in tracked:
                if not regclass_exists(cur, rel):
                    continue
                schema, table = rel.split(".", 1)
                info = fetch_one(
                    cur,
                    """
                    SELECT pg_total_relation_size(%s::regclass)::bigint AS total_bytes,
                           pg_relation_size(%s::regclass)::bigint AS heap_bytes,
                           pg_indexes_size(%s::regclass)::bigint AS index_bytes
                    """,
                    (rel, rel, rel),
                )
                if args.deep:
                    exact = fetch_one(cur, f"SELECT count(*)::bigint AS rows FROM {qname(schema, table)}")
                    info.update(exact)
                else:
                    approx = fetch_one(
                        cur,
                        """
                        SELECT COALESCE(s.n_live_tup, c.reltuples)::bigint AS estimated_rows
                        FROM pg_class c
                        JOIN pg_namespace n ON n.oid=c.relnamespace
                        LEFT JOIN pg_stat_user_tables s ON s.relid=c.oid
                        WHERE n.nspname=%s AND c.relname=%s
                        """,
                        (schema, table),
                    )
                    info.update(approx)
                report["payloads"][rel] = info

            if regclass_exists(cur, "processing.pipeline_run"):
                run_summary = fetch_all(
                    cur,
                    """
                    SELECT source_group, status, stage, count(*)::bigint AS runs,
                           min(started_at) AS oldest,
                           max(started_at) AS newest
                    FROM processing.pipeline_run
                    GROUP BY source_group, status, stage
                    ORDER BY source_group, status, stage
                    """,
                )
                report["pipeline_runs"] = {"summary": run_summary}

                if regclass_exists(cur, "processing.candidate_edge"):
                    per_run = fetch_all(
                        cur,
                        """
                        SELECT pr.source_group, pr.run_id::text, pr.status, pr.stage, pr.started_at,
                               count(ce.*)::bigint AS candidate_edges
                        FROM processing.pipeline_run pr
                        LEFT JOIN processing.candidate_edge ce ON ce.run_id=pr.run_id
                        GROUP BY pr.source_group, pr.run_id, pr.status, pr.stage, pr.started_at
                        ORDER BY pr.started_at DESC
                        LIMIT 50
                        """,
                    )
                    report["pipeline_runs"]["recent_edge_counts"] = per_run

            # DB bloat/space clues from statistics (no mutation).
            stats = fetch_all(
                cur,
                """
                SELECT schemaname, relname, n_live_tup::bigint, n_dead_tup::bigint,
                       last_vacuum, last_autovacuum, last_analyze, last_autoanalyze
                FROM pg_stat_user_tables
                ORDER BY n_dead_tup DESC NULLS LAST
                LIMIT 30
                """,
            )
            report["dead_tuple_stats"] = stats

    db_bytes = int(report["database_summary"].get("database_bytes") or 0)
    silver = report["payloads"].get(f"{settings.silver_schema}.{settings.image_table}", {})
    hot = report["payloads"].get("processing.gambar_reused", {})
    emb = report["payloads"].get("processing.image_feature", {})
    raw_image_bytes = int(silver.get("image_payload_bytes") or 0)
    hot_image_bytes = int(hot.get("image_payload_bytes") or 0)
    emb_bytes = int(emb.get("logical_embedding_est_bytes") or 0)

    if raw_image_bytes:
        report["notes"].append(
            "silver image_bytes masih menyimpan payload gambar penuh. Ini kandidat utama untuk migrasi NAS-first setelah coverage/path/hash diverifikasi."
        )
    if hot_image_bytes:
        report["notes"].append(
            "processing.gambar_reused adalah hot/evidence cache; ukurannya seharusnya jauh lebih kecil daripada seluruh corpus NAS."
        )
    report["notes"].append(
        "processing.image_feature berisi embedding float16; logical payload umumnya jauh lebih kecil daripada file JPEG asli."
    )
    report["notes"].append(
        "Jangan DELETE/TRUNCATE apa pun dari hasil audit ini. Bersihkan hanya setelah dependency/coverage final diverifikasi."
    )

    # Write CSV relations.
    with csv_path.open("w", newline="", encoding="utf-8-sig") as f:
        fields = ["schema_name", "relation_name", "relation_type", "estimated_rows", "total_bytes", "heap_bytes", "index_bytes", "toast_bytes"]
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for row in report["relations"]:
            w.writerow({k: row.get(k) for k in fields})

    # JSON needs default string for datetimes.
    json_path.write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str), encoding="utf-8")

    lines = []
    lines.append("=" * 88)
    lines.append("GOLDEN EYE - POSTGRESQL STORAGE AUDIT (READ ONLY)")
    lines.append("=" * 88)
    lines.append(f"Database : {report['database_summary'].get('database_name')}")
    lines.append(f"PG       : {report['database_summary'].get('server_version')}")
    lines.append(f"DB Size  : {report['database_summary'].get('database_size')} ({db_bytes:,} bytes)")
    lines.append(f"Mode     : {'DEEP exact counts' if args.deep else 'SAFE catalog + targeted exact payload totals'}")
    lines.append("")
    lines.append("TOP RELATIONS")
    lines.append("-" * 88)
    for row in report["relations"]:
        name = f"{row['schema_name']}.{row['relation_name']}"
        pct = (100.0 * int(row['total_bytes']) / db_bytes) if db_bytes else 0.0
        lines.append(
            f"{name:<46} total={human_bytes(row['total_bytes']):>11} "
            f"toast={human_bytes(row['toast_bytes']):>11} idx={human_bytes(row['index_bytes']):>11} "
            f"rows~={int(row.get('estimated_rows') or 0):>11,} {pct:6.2f}%"
        )
    lines.append("")
    lines.append("PAYLOAD BREAKDOWN")
    lines.append("-" * 88)
    if silver:
        lines.append(f"Silver raw images  : rows={int(silver.get('rows') or 0):,} bytea_rows={int(silver.get('image_bytea_rows') or 0):,} payload={human_bytes(raw_image_bytes)}")
        lines.append(f"Metadata-only rows : {int(silver.get('metadata_only_rows') or 0):,}")
    if hot:
        lines.append(f"Hot reused evidence: rows={int(hot.get('rows') or 0):,} active={int(hot.get('active_rows') or 0):,} payload={human_bytes(hot_image_bytes)}")
    if emb:
        lines.append(f"Image embeddings   : rows={int(emb.get('rows') or 0):,} dims={emb.get('min_dim')}..{emb.get('max_dim')} logical_payload~={human_bytes(emb_bytes)}")
        lines.append(f"                    full={human_bytes(emb.get('full_embedding_est_bytes'))} center={human_bytes(emb.get('center_embedding_est_bytes'))}")
    lines.append("")
    lines.append("INTERMEDIATE / FINAL TABLES")
    lines.append("-" * 88)
    for rel in tracked:
        info = report["payloads"].get(rel)
        if not info:
            continue
        rows_key = "rows" if "rows" in info else "estimated_rows"
        lines.append(f"{rel:<46} {human_bytes(info.get('total_bytes')):>11} rows{'=' if rows_key=='rows' else '~='}{int(info.get(rows_key) or 0):,}")
    lines.append("")
    lines.append("BYTEA COLUMNS FOUND")
    lines.append("-" * 88)
    for row in report.get("bytea_columns", []):
        lines.append(f"{row['table_schema']}.{row['table_name']}.{row['column_name']}")
    lines.append("")
    lines.append("INITIAL INTERPRETATION")
    lines.append("-" * 88)
    lines.append(f"Full-image payload in Silver : {human_bytes(raw_image_bytes)}")
    lines.append(f"Hot evidence image payload   : {human_bytes(hot_image_bytes)}")
    lines.append(f"Logical embedding payload    : {human_bytes(emb_bytes)}")
    if raw_image_bytes and emb_bytes:
        lines.append(f"Image/embedding ratio        : {raw_image_bytes / max(1, emb_bytes):,.1f}x")
    lines.append("")
    for note in report["notes"]:
        lines.append("- " + note)
    lines.append("")
    lines.append(f"Relations CSV : {csv_path}")
    lines.append(f"Full JSON     : {json_path}")
    lines.append("=" * 88)

    text = "\n".join(lines)
    txt_path.write_text(text, encoding="utf-8")
    print(text)
    print(f"\nREPORT_TXT={txt_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
