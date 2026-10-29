from __future__ import annotations

import json
import uuid
from typing import Any

from .config import Settings
from .db import connection


MATCH_STAGING_TABLES = (
    "candidate_group_member",
    "candidate_edge",
    "candidate_group",
)


def reset_match_staging(
    settings: Settings,
    run_id: str | uuid.UUID,
    *,
    apply: bool = False,
) -> dict[str, Any]:
    """Buat ulang relfilenode staging MATCH tanpa menyentuh hasil bisnis.

    Candidate edge/group/member hanya merupakan hasil sementara sebelum
    VALIDATE. ``TRUNCATE`` membentuk heap dan index kosong baru tanpa perlu
    membaca page lama yang rusak. Image feature, Context, validated result,
    Gold aktif, dan gambar_reused tidak disentuh.
    """
    selected_run_id = uuid.UUID(str(run_id))
    with connection(settings) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT source_group, stage, status, error_message
            FROM processing.pipeline_run
            WHERE run_id=%s
            """,
            (selected_run_id,),
        )
        run_row = cur.fetchone()
        if run_row is None:
            raise RuntimeError(f"Run tidak ditemukan: {selected_run_id}")

        # Metadata katalog tidak membaca heap/index yang rusak. Ini membantu
        # memetakan filenode dari pesan base/<db_oid>/<relfilenode>.
        cur.execute(
            """
            SELECT
                n.nspname,
                c.relname,
                c.relkind,
                pg_relation_filepath(c.oid),
                c.relpages::bigint,
                c.reltuples::bigint
            FROM pg_class c
            JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname='processing'
              AND (
                    c.relname=ANY(%s)
                    OR c.oid IN (
                        SELECT i.indexrelid
                        FROM pg_index i
                        JOIN pg_class t ON t.oid=i.indrelid
                        JOIN pg_namespace tn ON tn.oid=t.relnamespace
                        WHERE tn.nspname='processing'
                          AND t.relname=ANY(%s)
                    )
              )
            ORDER BY c.relkind, c.relname
            """,
            (list(MATCH_STAGING_TABLES), list(MATCH_STAGING_TABLES)),
        )
        relations = [
            {
                "schema": str(row[0]),
                "relation": str(row[1]),
                "kind": str(row[2]),
                "path": str(row[3]),
                "estimated_pages": int(row[4] or 0),
                "estimated_rows": int(row[5] or 0),
            }
            for row in cur.fetchall()
        ]

    print("=" * 78)
    print(f"MATCH STAGING STORAGE RESET | run_id={selected_run_id}")
    print("=" * 78)
    print(
        f"source_group={run_row[0]} stage={run_row[1]} status={run_row[2]} "
        f"apply={apply}"
    )
    for item in relations:
        print(
            f"  {item['kind']} {item['schema']}.{item['relation']} "
            f"path={item['path']} pages~={item['estimated_pages']:,}"
        )

    if not apply:
        print(
            "DRY-RUN: belum ada perubahan. --apply akan mengosongkan hanya "
            "candidate_edge/candidate_group/candidate_group_member untuk semua run."
        )
        print("Embedding, Context, validated result, Gold, dan BYTEA tetap utuh.")
        return {
            "run_id": str(selected_run_id),
            "source_group": str(run_row[0]),
            "relations": relations,
            "applied": False,
        }

    with connection(settings) as conn, conn.cursor() as cur:
        cur.execute("SET statement_timeout=0")
        cur.execute(
            """
            TRUNCATE TABLE
                processing.candidate_group_member,
                processing.candidate_edge,
                processing.candidate_group
            """
        )
        cur.execute("ANALYZE processing.candidate_edge")
        cur.execute("ANALYZE processing.candidate_group")
        cur.execute("ANALYZE processing.candidate_group_member")
        cur.execute(
            """
            UPDATE processing.pipeline_run
            SET stage='MATCH',
                status='PAUSED',
                error_message=NULL,
                finished_at=NULL,
                metrics=metrics || %s::jsonb,
                updated_at=now()
            WHERE run_id=%s
            """,
            (
                json.dumps(
                    {
                        "match_staging_storage_reset": True,
                        "match_staging_resume_from": "match",
                    }
                ),
                selected_run_id,
            ),
        )
        if cur.rowcount != 1:
            raise RuntimeError(
                f"Run gagal ditandai siap resume: {selected_run_id}"
            )

    print(
        "OK storage candidate MATCH dibuat kosong kembali; embedding/Context/"
        "validated/Gold/BYTEA tidak diubah."
    )
    print(
        f"RESUME run_id={selected_run_id} dari --from-stage match"
    )
    return {
        "run_id": str(selected_run_id),
        "source_group": str(run_row[0]),
        "relations": relations,
        "applied": True,
    }
