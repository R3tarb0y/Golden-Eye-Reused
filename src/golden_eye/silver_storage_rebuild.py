from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path
from typing import Any

from psycopg2 import sql

from .config import Settings
from .db import connection


def _qualified(schema: str, table: str) -> sql.Composed:
    return sql.SQL("{}.{}").format(sql.Identifier(schema), sql.Identifier(table))


def _copy_batch(
    cur: Any,
    *,
    schema: str,
    active_table: str,
    stage_table: str,
    ctids: list[str],
    metadata: dict[str, dict[str, str]],
    skipped: list[dict[str, str]],
) -> int:
    """Copy healthy rows and isolate unreadable heap/TOAST rows recursively."""
    if not ctids:
        return 0
    placeholders = sql.SQL(", ").join(sql.Placeholder() for _ in ctids)
    statement = sql.SQL(
        """
        INSERT INTO {stage}
        SELECT source_row.*
        FROM {active} AS source_row
        WHERE source_row.ctid IN ({ctids})
        ON CONFLICT (file_uid) DO NOTHING
        """
    ).format(
        stage=_qualified(schema, stage_table),
        active=_qualified(schema, active_table),
        ctids=placeholders,
    )
    cur.execute("SAVEPOINT copy_silver_storage_batch")
    try:
        cur.execute(statement, ctids)
        inserted = max(0, int(cur.rowcount))
        cur.execute("RELEASE SAVEPOINT copy_silver_storage_batch")
        return inserted
    except Exception as exc:
        cur.execute("ROLLBACK TO SAVEPOINT copy_silver_storage_batch")
        cur.execute("RELEASE SAVEPOINT copy_silver_storage_batch")
        if len(ctids) > 1:
            middle = len(ctids) // 2
            return _copy_batch(
                cur,
                schema=schema,
                active_table=active_table,
                stage_table=stage_table,
                ctids=ctids[:middle],
                metadata=metadata,
                skipped=skipped,
            ) + _copy_batch(
                cur,
                schema=schema,
                active_table=active_table,
                stage_table=stage_table,
                ctids=ctids[middle:],
                metadata=metadata,
                skipped=skipped,
            )
        item = metadata[ctids[0]]
        skipped.append(
            {
                **item,
                "status": "SILVER_ROW_COPY_FAILED",
                "error": " ".join(str(exc).split())[:1000],
            }
        )
        return 0


def rebuild_silver_storage(
    settings: Settings,
    *,
    batch_size: int = 64,
    report_path: str | None = None,
    apply: bool = False,
) -> dict[str, Any]:
    """Move readable Silver rows to a fresh heap/TOAST relation and swap safely.

    The corrupt table is retained under a timestamped quarantine name. Rows
    whose BYTEA cannot be detoasted are omitted from the fresh table and listed
    by stable file_uid/file_name so load-silver/merge-month can recover them
    from NAS without creating duplicates.
    """
    if not 1 <= int(batch_size) <= 256:
        raise ValueError("--batch-size rebuild-silver-storage harus 1..256")
    schema = settings.silver_schema
    active_table = settings.image_table
    stage_table = active_table + "_rebuild"
    output = Path(
        report_path or "silver_storage_rebuild_recovery_needed.csv"
    ).resolve()

    with connection(settings) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT to_regclass(%s), to_regclass(%s)",
            (f"{schema}.{active_table}", f"{schema}.{stage_table}"),
        )
        active_regclass, stage_regclass = cur.fetchone()
        if active_regclass is None:
            raise RuntimeError(f"{schema}.{active_table} tidak ditemukan")
        # Index source juga dapat rusak. Count preflight wajib membaca heap
        # metadata langsung dan tidak boleh dipilih planner lewat index lama.
        cur.execute("SET LOCAL enable_indexscan=off")
        cur.execute("SET LOCAL enable_indexonlyscan=off")
        cur.execute("SET LOCAL enable_bitmapscan=off")
        cur.execute(
            sql.SQL(
                "SELECT count(*)::bigint, pg_total_relation_size(%s::regclass), "
                "pg_size_pretty(pg_total_relation_size(%s::regclass)) FROM {}"
            ).format(_qualified(schema, active_table)),
            (f"{schema}.{active_table}", f"{schema}.{active_table}"),
        )
        active_rows, total_bytes, pretty_size = cur.fetchone()
        stage_rows = 0
        if stage_regclass is not None:
            cur.execute(
                sql.SQL("SELECT count(*)::bigint FROM {}").format(
                    _qualified(schema, stage_table)
                )
            )
            stage_rows = int(cur.fetchone()[0])

    print("=" * 78)
    print("SILVER PHYSICAL STORAGE REBUILD (HEAP/TOAST SAFE COPY)")
    print("=" * 78)
    print(
        f"source={schema}.{active_table} rows={int(active_rows):,} "
        f"total_size={pretty_size} stage_rows={stage_rows:,} apply={apply}"
    )
    print(
        "Tabel lama tidak dihapus. Sediakan ruang sementara minimal sekitar "
        f"{max(1, int(total_bytes * 1.30 / (1024 ** 3)))} GB."
    )
    if not apply:
        print("DRY-RUN saja; belum membuat/copy/swap relation.")
        return {
            "source_rows": int(active_rows),
            "source_total_bytes": int(total_bytes),
            "stage_rows": stage_rows,
            "applied": False,
            "report": str(output),
        }

    skipped: list[dict[str, str]] = []
    inserted = 0
    backup_name = ""
    with connection(settings) as conn, conn.cursor() as cur:
        cur.execute("SET statement_timeout=0")
        cur.execute(
            sql.SQL("CREATE TABLE IF NOT EXISTS {} (LIKE {} INCLUDING ALL)").format(
                _qualified(schema, stage_table),
                _qualified(schema, active_table),
            )
        )
        conn.commit()

        # Kolom kecil ini berada di heap utama dan tidak memaksa detoast BYTEA.
        # Kasus yang ditangani command ini adalah TOAST rusak dengan heap sehat.
        cur.execute(
            sql.SQL(
                "SELECT ctid::text, file_uid::text, source_group::text, "
                "file_name::text FROM {} ORDER BY ctid"
            ).format(_qualified(schema, active_table))
        )
        source_rows = cur.fetchall()
        metadata = {
            str(row[0]): {
                "ctid": str(row[0]),
                "file_uid": str(row[1]),
                "source_group": str(row[2]),
                "file_name": str(row[3]),
            }
            for row in source_rows
        }
        if len(metadata) != int(active_rows):
            raise RuntimeError(
                "Metadata heap Silver tidak konsisten; swap dibatalkan: "
                f"count={int(active_rows):,} metadata={len(metadata):,}. "
                "Command ini hanya aman untuk TOAST rusak dengan heap sehat."
            )
        cur.execute(
            sql.SQL("SELECT file_uid::text FROM {}").format(
                _qualified(schema, stage_table)
            )
        )
        staged_uids = {str(row[0]) for row in cur.fetchall()}
        source_uids = {item["file_uid"] for item in metadata.values()}
        extra_stage = staged_uids - source_uids
        if extra_stage:
            raise RuntimeError(
                f"Stage {schema}.{stage_table} mempunyai {len(extra_stage):,} "
                "file_uid yang tidak ada pada source. Jangan swap; hapus stage "
                "lama hanya setelah targetnya dipastikan."
            )
        pending = [
            ctid
            for ctid, item in metadata.items()
            if item["file_uid"] not in staged_uids
        ]
        print(
            f"[SILVER REBUILD] source={len(metadata):,} "
            f"existing_stage={len(staged_uids):,} pending={len(pending):,}",
            flush=True,
        )
        for start in range(0, len(pending), int(batch_size)):
            ctids = pending[start : start + int(batch_size)]
            inserted += _copy_batch(
                cur,
                schema=schema,
                active_table=active_table,
                stage_table=stage_table,
                ctids=ctids,
                metadata=metadata,
                skipped=skipped,
            )
            conn.commit()
            checked = min(start + len(ctids), len(pending))
            if checked % 1024 == 0 or checked == len(pending):
                print(
                    f"[SILVER REBUILD] checked={checked:,}/{len(pending):,} "
                    f"inserted={inserted:,} skipped={len(skipped):,}",
                    flush=True,
                )

        cur.execute(
            sql.SQL("SELECT count(*)::bigint FROM {}").format(
                _qualified(schema, stage_table)
            )
        )
        rebuilt_rows = int(cur.fetchone()[0])
        if rebuilt_rows + len(skipped) != len(metadata):
            raise RuntimeError(
                "Coverage staging tidak konsisten; swap dibatalkan: "
                f"source={len(metadata):,} rebuilt={rebuilt_rows:,} "
                f"skipped={len(skipped):,}"
            )

        backup_name = (
            active_table + "_corrupt_" + datetime.now().strftime("%Y%m%d_%H%M%S")
        )
        cur.execute(
            sql.SQL("LOCK TABLE {} IN ACCESS EXCLUSIVE MODE").format(
                _qualified(schema, active_table)
            )
        )
        cur.execute(
            sql.SQL("LOCK TABLE {} IN ACCESS EXCLUSIVE MODE").format(
                _qualified(schema, stage_table)
            )
        )
        cur.execute(
            sql.SQL("ALTER TABLE {} RENAME TO {}").format(
                _qualified(schema, active_table), sql.Identifier(backup_name)
            )
        )
        cur.execute(
            sql.SQL("ALTER TABLE {} RENAME TO {}").format(
                _qualified(schema, stage_table), sql.Identifier(active_table)
            )
        )
        cur.execute(
            sql.SQL("ANALYZE {}").format(_qualified(schema, active_table))
        )

    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "ctid",
                "file_uid",
                "source_group",
                "file_name",
                "status",
                "error",
            ],
        )
        writer.writeheader()
        writer.writerows(skipped)

    print(
        f"[SILVER REBUILD] SWAP OK source={len(metadata):,} "
        f"new_rows={len(metadata) - len(skipped):,} skipped={len(skipped):,} "
        f"backup={schema}.{backup_name} report={output}",
        flush=True,
    )
    print(
        "Jangan DROP backup sebelum merge-month, audit BYTEA, dan recovery NAS lulus.",
        flush=True,
    )
    return {
        "source_rows": len(metadata),
        "new_rows": len(metadata) - len(skipped),
        "skipped": len(skipped),
        "backup_table": f"{schema}.{backup_name}",
        "applied": True,
        "report": str(output),
    }
