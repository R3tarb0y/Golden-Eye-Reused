from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path
from typing import Any

from psycopg2 import sql

from .config import Settings
from .db import connection


SCHEMA = "processing"
ACTIVE_TABLE = "gambar_reused"
STAGE_TABLE = "gambar_reused_rebuild"


def _qualified(schema: str, table: str) -> sql.Composed:
    return sql.SQL("{}.{}").format(sql.Identifier(schema), sql.Identifier(table))


def _copy_batch(
    cur: Any,
    ctids: list[str],
    metadata: dict[str, dict[str, str]],
    skipped: list[dict[str, str]],
) -> int:
    if not ctids:
        return 0
    placeholders = sql.SQL(", ").join(sql.Placeholder() for _ in ctids)
    statement = sql.SQL(
        """
        INSERT INTO {stage}
        SELECT source_row.*
        FROM {active} AS source_row
        WHERE source_row.ctid IN ({ctids})
        ON CONFLICT (source_group, file_uid) DO NOTHING
        """
    ).format(
        stage=_qualified(SCHEMA, STAGE_TABLE),
        active=_qualified(SCHEMA, ACTIVE_TABLE),
        ctids=placeholders,
    )
    cur.execute("SAVEPOINT copy_hot_store_batch")
    try:
        cur.execute(statement, ctids)
        inserted = max(0, int(cur.rowcount))
        cur.execute("RELEASE SAVEPOINT copy_hot_store_batch")
        return inserted
    except Exception as exc:
        cur.execute("ROLLBACK TO SAVEPOINT copy_hot_store_batch")
        cur.execute("RELEASE SAVEPOINT copy_hot_store_batch")
        if len(ctids) > 1:
            middle = len(ctids) // 2
            return _copy_batch(cur, ctids[:middle], metadata, skipped) + _copy_batch(
                cur, ctids[middle:], metadata, skipped
            )
        item = metadata[ctids[0]]
        skipped.append(
            {
                **item,
                "status": "BYTEA_COPY_FAILED",
                "error": " ".join(str(exc).split())[:1000],
            }
        )
        return 0


def rebuild_gambar_reused_storage(
    settings: Settings,
    *,
    batch_size: int = 128,
    report_path: str | None = None,
    apply: bool = False,
) -> dict[str, Any]:
    """Rebuild hot BYTEA heap/TOAST without reading Silver or deleting source.

    Healthy payloads are copied server-side into a fresh relation. Rows whose
    BYTEA cannot be detoasted are reported and omitted, then the fresh relation
    is swapped atomically into the canonical name. The corrupt source remains
    as a timestamped quarantine table until the user verifies recovery.
    """
    if not 1 <= int(batch_size) <= 512:
        raise ValueError("--batch-size harus 1..512")
    output = Path(report_path or "gambar_reused_storage_rebuild_skipped.csv").resolve()

    with connection(settings) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT to_regclass('processing.gambar_reused'),
                   to_regclass('processing.gambar_reused_rebuild')
            """
        )
        active_regclass, stage_regclass = cur.fetchone()
        if active_regclass is None:
            raise RuntimeError("processing.gambar_reused tidak ditemukan")
        cur.execute(
            """
            SELECT count(*)::bigint,
                   pg_total_relation_size('processing.gambar_reused'::regclass),
                   pg_size_pretty(
                       pg_total_relation_size('processing.gambar_reused'::regclass)
                   )
            FROM processing.gambar_reused
            """
        )
        active_rows, total_bytes, pretty_size = cur.fetchone()
        stage_rows = 0
        if stage_regclass is not None:
            cur.execute("SELECT count(*)::bigint FROM processing.gambar_reused_rebuild")
            stage_rows = int(cur.fetchone()[0])

    print("=" * 78)
    print("GAMBAR_REUSED PHYSICAL STORAGE REBUILD")
    print("=" * 78)
    print(
        f"active_rows={int(active_rows):,} active_total_size={pretty_size} "
        f"stage_rows={stage_rows:,} apply={apply}"
    )
    print(
        "Syarat ruang sementara: sediakan minimal sekitar "
        f"{max(1, int(total_bytes * 1.25 / (1024 ** 3)))} GB kosong."
    )
    if not apply:
        print("DRY-RUN saja; tabel tidak dibuat, data tidak disalin, dan nama tidak diubah.")
        return {
            "active_rows": int(active_rows),
            "active_total_bytes": int(total_bytes),
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
            """
            CREATE TABLE IF NOT EXISTS processing.gambar_reused_rebuild
            (LIKE processing.gambar_reused INCLUDING ALL)
            """
        )
        conn.commit()

        # Metadata kecil tidak mendetost image_bytes; aman untuk menentukan
        # identitas dan ctid setiap row sumber.
        cur.execute(
            """
            SELECT ctid::text, source_group, file_uid, file_name
            FROM processing.gambar_reused
            """
        )
        source_rows = cur.fetchall()
        metadata = {
            str(row[0]): {
                "ctid": str(row[0]),
                "source_group": str(row[1]),
                "file_uid": str(row[2]),
                "file_name": str(row[3]),
            }
            for row in source_rows
        }
        cur.execute(
            """
            SELECT source_group, file_uid
            FROM processing.gambar_reused_rebuild
            """
        )
        staged = {(str(row[0]), str(row[1])) for row in cur.fetchall()}
        pending = [
            ctid
            for ctid, item in metadata.items()
            if (item["source_group"], item["file_uid"]) not in staged
        ]
        print(
            f"[HOT STORE REBUILD] source={len(metadata):,} existing_stage={len(staged):,} "
            f"pending={len(pending):,}",
            flush=True,
        )
        for start in range(0, len(pending), int(batch_size)):
            batch = pending[start : start + int(batch_size)]
            inserted += _copy_batch(cur, batch, metadata, skipped)
            conn.commit()
            checked = min(start + len(batch), len(pending))
            if checked % 1024 == 0 or checked == len(pending):
                print(
                    f"[HOT STORE REBUILD] checked={checked:,}/{len(pending):,} "
                    f"inserted={inserted:,} skipped={len(skipped):,}",
                    flush=True,
                )

        cur.execute("SELECT count(*)::bigint FROM processing.gambar_reused_rebuild")
        rebuilt_rows = int(cur.fetchone()[0])
        if rebuilt_rows + len(skipped) != len(metadata):
            raise RuntimeError(
                "Coverage staging tidak konsisten: "
                f"source={len(metadata)} rebuilt={rebuilt_rows} skipped={len(skipped)}"
            )

        backup_name = "gambar_reused_corrupt_" + datetime.now().strftime("%Y%m%d_%H%M%S")
        cur.execute("LOCK TABLE processing.gambar_reused IN ACCESS EXCLUSIVE MODE")
        cur.execute("LOCK TABLE processing.gambar_reused_rebuild IN ACCESS EXCLUSIVE MODE")
        cur.execute(
            sql.SQL("ALTER TABLE {} RENAME TO {}").format(
                _qualified(SCHEMA, ACTIVE_TABLE), sql.Identifier(backup_name)
            )
        )
        cur.execute(
            sql.SQL("ALTER TABLE {} RENAME TO {}").format(
                _qualified(SCHEMA, STAGE_TABLE), sql.Identifier(ACTIVE_TABLE)
            )
        )
        cur.execute("ANALYZE processing.gambar_reused")

    output.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = ["ctid", "source_group", "file_uid", "file_name", "status", "error"]
    with output.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(skipped)

    # V109: pecah recovery manifest per source-group. File ini kompatibel langsung
    # dengan recover_corrupt_bytea.py --input-report karena mempunyai file_name/file_uid.
    per_source_reports: dict[str, str] = {}
    for source_group in sorted({str(row.get("source_group") or "").upper() for row in skipped if row.get("source_group")}):
        rows = [row for row in skipped if str(row.get("source_group") or "").upper() == source_group]
        source_path = output.with_name(f"{output.stem}_{source_group}{output.suffix or '.csv'}")
        with source_path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
        per_source_reports[source_group] = str(source_path)

    # Metadata-only verification. New relation contains only rows whose full source_row
    # copy succeeded, therefore a corrupt source TOAST payload cannot be present here.
    with connection(settings) as verify_conn, verify_conn.cursor() as verify_cur:
        verify_cur.execute("SELECT count(*)::bigint FROM processing.gambar_reused")
        verified_rows = int(verify_cur.fetchone()[0])
        verify_cur.execute(
            """
            SELECT c.reltoastrelid::regclass::text,
                   pg_relation_filenode(c.oid),
                   CASE WHEN c.reltoastrelid <> 0 THEN pg_relation_filenode(c.reltoastrelid) END
            FROM pg_class c
            WHERE c.oid='processing.gambar_reused'::regclass
            """
        )
        toast_name, active_filenode, toast_filenode = verify_cur.fetchone()
    expected_rows = len(metadata) - len(skipped)
    if verified_rows != expected_rows:
        raise RuntimeError(
            f"Post-swap count tidak konsisten: expected={expected_rows} actual={verified_rows}"
        )

    print(
        f"[HOT STORE REBUILD] SWAP OK new_rows={verified_rows:,} "
        f"skipped={len(skipped):,} backup=processing.{backup_name} report={output}",
        flush=True,
    )
    print(
        f"[HOT STORE VERIFY] active_filenode={active_filenode} "
        f"toast={toast_name} toast_filenode={toast_filenode}",
        flush=True,
    )
    if per_source_reports:
        print("[HOT STORE RECOVERY MANIFEST]", flush=True)
        for source_group, path in per_source_reports.items():
            count = sum(1 for row in skipped if str(row.get("source_group") or "").upper() == source_group)
            print(f"  - {source_group}: {count:,} row -> {path}", flush=True)
        print(
            "Recovery berikutnya: .\run_report_recovery.ps1 -SourceGroup <GROUP> "
            "-NasRoot <ROOT_NAS> -InputReport <CSV_GROUP>",
            flush=True,
        )
    else:
        print("[HOT STORE RECOVERY] tidak ada row corrupt yang perlu dipulihkan dari NAS.", flush=True)
    print("Jangan DROP backup sebelum seluruh audit panen/transport dan report gambar lulus.")
    return {
        "source_rows": len(metadata),
        "new_rows": verified_rows,
        "skipped": len(skipped),
        "backup_table": f"processing.{backup_name}",
        "applied": True,
        "report": str(output),
        "per_source_reports": per_source_reports,
        "active_filenode": int(active_filenode) if active_filenode is not None else None,
        "toast_filenode": int(toast_filenode) if toast_filenode is not None else None,
    }
