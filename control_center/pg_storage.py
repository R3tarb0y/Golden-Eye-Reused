"""Fast PostgreSQL storage inventory and tightly guarded backup-table removal.

No row counts, toast payload reads or table scans are performed by inventory.
Destructive operations are opt-in, protected by a separate admin token, and
restricted to verified, explicitly named legacy corrupt-backup tables.
"""
from __future__ import annotations

import hmac
import os
import re
import secrets
import threading
import time
from typing import Any

from psycopg2 import sql
from golden_eye.config import Settings
from golden_eye.db import connection

SETTINGS = Settings()
_ALLOWED_BACKUP = re.compile(r"^image_store_all_files_corrupt_\d{8}_\d{6}(?:_\d+)?$")
_PREVIEWS: dict[str, dict[str, Any]] = {}
_PREVIEW_LOCK = threading.Lock()


def _protected_reason(schema: str, table: str) -> str:
    if schema == "silver" and _ALLOWED_BACKUP.fullmatch(table):
        return "BACKUP_REVIEW"
    if schema == "dashboard_cache" and table.startswith("snapshot_"):
        return "SNAPSHOT_MANAGED"
    if schema in {"gold", "raw", "silver", "processing", "control", "reference", "geo"}:
        return "PROTECTED_PIPELINE_OR_DASHBOARD"
    return "MANUAL_REVIEW"


def storage_report(limit: int = 80) -> dict[str, Any]:
    limit = max(1, min(200, int(limit)))
    with connection(SETTINGS) as conn, conn.cursor() as cur:
        cur.execute("SET LOCAL statement_timeout = '8s'")
        cur.execute("""
            SELECT current_database(), pg_database_size(current_database()),
                   current_setting('server_version')
        """)
        database, size, version = cur.fetchone()
        cur.execute("""
            SELECT n.nspname, c.relname, c.oid::bigint, c.relkind,
                   pg_total_relation_size(c.oid)::bigint,
                   pg_relation_size(c.oid)::bigint,
                   pg_indexes_size(c.oid)::bigint,
                   CASE WHEN c.reltoastrelid=0 THEN 0::bigint
                        ELSE pg_total_relation_size(c.reltoastrelid)::bigint END,
                   COALESCE(st.n_live_tup, GREATEST(c.reltuples,0))::bigint,
                   COALESCE(st.n_dead_tup,0)::bigint
            FROM pg_class c
            JOIN pg_namespace n ON n.oid=c.relnamespace
            LEFT JOIN pg_stat_user_tables st ON st.relid=c.oid
            WHERE c.relkind IN ('r','m')
              AND n.nspname NOT IN ('pg_catalog','information_schema')
              AND n.nspname NOT LIKE %s
            ORDER BY pg_total_relation_size(c.oid) DESC LIMIT %s
        """, ("pg_toast%", limit))
        relations = []
        for schema, name, oid, kind, total, heap, indexes, toast, rows, dead in cur.fetchall():
            relations.append({
                "schema": schema, "name": name, "relation": f"{schema}.{name}",
                "oid": int(oid), "kind": "materialized_view" if kind == "m" else "table",
                "total_bytes": int(total), "heap_bytes": int(heap),
                "index_bytes": int(indexes), "toast_bytes": int(toast),
                "estimated_rows": int(rows), "dead_tuples": int(dead),
                "policy": _protected_reason(schema, name),
            })
        cur.execute("SELECT to_regclass('dashboard_cache.active_snapshot') IS NOT NULL")
        has_cache = bool(cur.fetchone()[0])
        snapshots = []
        if has_cache:
            cur.execute("SELECT source_name, snapshot_name, refreshed_at, row_count FROM dashboard_cache.active_snapshot ORDER BY source_name")
            snapshots = [{"source": a,"table": b,"refreshed_at": c.isoformat() if c else None,"rows": d} for a,b,c,d in cur.fetchall()]
    return {"database": database, "database_bytes": int(size), "server_version": version,
            "relations": relations, "snapshots": snapshots,
            "deletion_enabled": bool(os.getenv("CONTROL_STORAGE_ALLOW_DROP_BACKUPS", "").lower() in ("1","true","yes") and os.getenv("CONTROL_STORAGE_ADMIN_TOKEN")),
            "note": "Ukuran relation mencakup heap, TOAST dan index. Dead tuples bukan ukuran ruang bebas yang bisa langsung diklaim. DROP hanya tersedia untuk backup corrupt legacy dengan otorisasi khusus."}


def _require_admin(value: str) -> None:
    expected = os.getenv("CONTROL_STORAGE_ADMIN_TOKEN", "")
    enabled = os.getenv("CONTROL_STORAGE_ALLOW_DROP_BACKUPS", "").lower() in ("1", "true", "yes")
    if not enabled or len(expected) < 24 or not hmac.compare_digest(value or "", expected):
        raise PermissionError("Penghapusan dinonaktifkan atau token admin storage salah (minimal 24 karakter).")


def _require_backup(relation: str) -> str:
    match = re.fullmatch(r"silver\.(image_store_all_files_corrupt_\d{8}_\d{6}(?:_\d+)?)", str(relation or ""))
    if not match:
        raise ValueError("Hanya backup silver.image_store_all_files_corrupt_YYYYMMDD_HHMMSS yang dapat dipertimbangkan untuk DROP.")
    return match.group(1)


def _running_jobs(cur) -> bool:
    cur.execute("SELECT to_regclass('control.pipeline_job')")
    if cur.fetchone()[0] is None:
        return False
    cur.execute("SELECT EXISTS(SELECT 1 FROM control.pipeline_job WHERE status IN ('RUNNING','PENDING') LIMIT 1)")
    return bool(cur.fetchone()[0])


def preview_drop_backup(relation: str, admin_token: str, acknowledgement: bool) -> dict[str, Any]:
    _require_admin(admin_token)
    name = _require_backup(relation)
    if not acknowledgement:
        raise ValueError("Konfirmasi bahwa backup sudah diverifikasi secara terpisah dan tidak berisi data unik sebelum meminta preview.")
    with connection(SETTINGS) as conn, conn.cursor() as cur:
        cur.execute("SET LOCAL statement_timeout='5s'")
        if _running_jobs(cur):
            raise ValueError("Ada job Control Center aktif/pending. Penghapusan dilarang selama pipeline berjalan.")
        cur.execute("""SELECT c.oid::bigint, c.relkind, pg_total_relation_size(c.oid)::bigint
                       FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                       WHERE n.nspname='silver' AND c.relname=%s""", (name,))
        r = cur.fetchone()
        if not r or r[1] != 'r':
            raise ValueError("Tabel backup tidak ditemukan atau bukan tabel reguler.")
    key = secrets.token_urlsafe(32)
    ticket = {"relation": relation, "name": name, "oid": int(r[0]), "total_bytes": int(r[2]), "expires_at": time.monotonic() + 180}
    with _PREVIEW_LOCK:
        _PREVIEWS.clear()
        _PREVIEWS[key] = ticket
    return {"preview_token": key, "relation": relation, "total_bytes": ticket["total_bytes"],
            "expires_seconds": 180, "warning": "DROP TABLE RESTRICT bersifat permanen. Sistem TIDAK dapat membuktikan isi backup sudah ada di NAS. Pastikan salinan terverifikasi terlebih dahulu."}


def confirm_drop_backup(relation: str, preview_token: str, typed_name: str, admin_token: str) -> dict[str, Any]:
    _require_admin(admin_token)
    name = _require_backup(relation)
    if typed_name != relation:
        raise ValueError("Nama tabel yang diketik harus sama persis dengan relation yang akan dihapus.")
    with _PREVIEW_LOCK:
        ticket = _PREVIEWS.pop(preview_token, None)
    if not ticket or ticket["relation"] != relation or time.monotonic() > ticket["expires_at"]:
        raise ValueError("Preview sudah kedaluwarsa. Lakukan preview ulang.")
    with connection(SETTINGS) as conn, conn.cursor() as cur:
        cur.execute("SET LOCAL lock_timeout='3s'")
        cur.execute("SET LOCAL statement_timeout='8s'")
        if _running_jobs(cur):
            raise ValueError("Job masih aktif. STOP dan verifikasi ulang sebelum menghapus backup.")
        cur.execute("""SELECT c.oid::bigint, pg_total_relation_size(c.oid)::bigint
                       FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                       WHERE n.nspname='silver' AND c.relname=%s AND c.relkind='r'""", (name,))
        r = cur.fetchone()
        if not r or int(r[0]) != ticket["oid"] or int(r[1]) != ticket["total_bytes"]:
            raise ValueError("OID atau ukuran tabel berubah setelah preview. Penghapusan dibatalkan.")
        # RESTRICT aborts on dependencies; do not CASCADE; do not touch active relations.
        cur.execute(sql.SQL("DROP TABLE {}.{} RESTRICT").format(sql.Identifier("silver"), sql.Identifier(name)))
    return {"ok": True, "deleted": relation, "previous_total_bytes": ticket["total_bytes"],
            "note": "DROP committed. Cek ulang ukuran DB; ini bukan penghapusan data Gold aktif."}
